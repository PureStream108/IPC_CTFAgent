"use strict";

const http = require("node:http");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const PORT = Number(process.env.IPC_RUNNER_PORT || 8600);
const WORKSPACE = process.env.IPC_WORKSPACE || "/data/ctf/IPC_CTFAgent";
const RUNNER_TOKEN = process.env.IPC_RUNNER_TOKEN || "";
const MAX_BODY_BYTES = 2 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 4 * 1024 * 1024;
const MAX_STREAM_EVENT_BYTES = 64 * 1024;
const MAX_CONCURRENT_RUNS = Number(process.env.IPC_RUNNER_MAX_CONCURRENT || 2);
const activeRuns = new Map();

function sendJson(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
    "Cache-Control": "no-store",
  });
  response.end(body);
}

function authorized(request) {
  if (!RUNNER_TOKEN) return true;
  const supplied = String(request.headers["x-ipc-runner-token"] || "");
  const expected = Buffer.from(RUNNER_TOKEN);
  const actual = Buffer.from(supplied);
  return expected.length === actual.length && crypto.timingSafeEqual(expected, actual);
}

function readJson(request) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    request.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("request body is too large"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => {
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch (error) {
        reject(new Error(`invalid JSON request: ${error.message}`));
      }
    });
    request.on("error", reject);
  });
}

function anthropicBaseUrl(value) {
  const base = String(value || "").trim().replace(/\/+$/, "");
  if (!base) throw new Error("base_url is required");
  // DeepSeek exposes an Anthropic-compatible surface below /anthropic while
  // the IPC config historically stores its OpenAI-compatible root URL.
  if (/^https:\/\/api\.deepseek\.com$/i.test(base)) return `${base}/anthropic`;
  return base;
}

function textField(value, name, maxLength) {
  if (typeof value !== "string" || !value.trim()) throw new Error(`${name} is required`);
  if (value.length > maxLength) throw new Error(`${name} is too long`);
  return value;
}

function boundedInteger(value, fallback, minimum, maximum) {
  const number = Number.isFinite(Number(value)) ? Math.trunc(Number(value)) : fallback;
  return Math.max(minimum, Math.min(maximum, number));
}

function runId(value) {
  if (typeof value === "string" && /^run_[A-Za-z0-9_-]{8,128}$/.test(value)) return value;
  return `run_${crypto.randomUUID().replaceAll("-", "")}`;
}

function resumeSessionId(value) {
  if (value == null || value === "") return null;
  const sessionId = String(value).trim();
  if (!/^[A-Za-z0-9_-]{8,128}$/.test(sessionId)) {
    throw new Error("resume_session_id is invalid");
  }
  return sessionId;
}

function requestCancellation(value) {
  const record = activeRuns.get(value);
  if (!record) return { ok: false, run_id: value, status: "not_found" };
  if (!record.interrupted) {
    record.interrupted = true;
    terminateChild(record.child, "SIGTERM");
    setTimeout(() => {
      if (activeRuns.has(value)) terminateChild(record.child, "SIGKILL");
    }, 5000).unref();
  }
  return { ok: true, run_id: value, status: "interrupting" };
}

function terminateChild(child, signal) {
  if (!child || !child.pid) return;
  try {
    // Claude can have Bash/MCP descendants. It runs in its own process group so
    // an IPC interrupt stops the whole action, not only the CLI parent.
    process.kill(-child.pid, signal);
  } catch (_) {
    try { child.kill(signal); } catch (_) {}
  }
}

function runnerMcpConfig(sessionId) {
  const url = String(process.env.IPC_IPC_MCP_URL || "http://ipc-app:8000/internal/mcp").trim();
  if (!RUNNER_TOKEN || !url) return null;
  const headers = { "X-IPC-Runner-Token": "${IPC_RUNNER_TOKEN}" };
  if (typeof sessionId === "string" && /^ops_[a-f0-9]{16}$/.test(sessionId)) {
    headers["X-IPC-Ops-Session"] = sessionId;
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "ipc-claude-mcp-"));
  const configPath = path.join(directory, "mcp.json");
  fs.writeFileSync(configPath, JSON.stringify({
    mcpServers: {
      ipc: { type: "http", url, headers },
    },
  }), { encoding: "utf8", mode: 0o600 });
  return {
    path: configPath,
    cleanup: () => fs.rmSync(directory, { recursive: true, force: true }),
  };
}

function appendOutput(current, chunk) {
  if (current.length >= MAX_OUTPUT_BYTES) return current;
  const next = current + chunk.toString("utf8");
  return next.length > MAX_OUTPUT_BYTES ? next.slice(0, MAX_OUTPUT_BYTES) : next;
}

function writeNdjson(response, value) {
  if (response.writableEnded) return;
  let body;
  try {
    body = JSON.stringify(value);
  } catch (_) {
    body = JSON.stringify({ type: "error", error: "runner could not encode an event" });
  }
  if (Buffer.byteLength(body, "utf8") > MAX_STREAM_EVENT_BYTES) {
    body = JSON.stringify({
      type: "event",
      event: { type: "runner", subtype: "truncated", text: "Claude event exceeded the display limit" },
    });
  }
  response.write(`${body}\n`);
}

function claudeInvocation(body, outputFormat) {
  const prompt = textField(body.prompt, "prompt", 120000);
  const apiKey = textField(body.api_key, "api_key", 16384);
  const baseUrl = anthropicBaseUrl(body.base_url);
  const model = textField(body.model || "deepseek-v4-flash", "model", 256);
  const maxTurns = boundedInteger(body.max_turns, 32, 1, 100);
  const timeoutMs = boundedInteger(body.timeout_ms, 900000, 60000, 1800000);
  const resume = resumeSessionId(body.resume_session_id);
  const mcp = runnerMcpConfig(body.session_id);
  const args = [
    "-p",
    prompt,
    "--output-format",
    outputFormat,
    "--dangerously-skip-permissions",
    "--max-turns",
    String(maxTurns),
    "--model",
    model,
  ];
  if (outputFormat === "stream-json") {
    args.push("--verbose", "--include-hook-events");
  }
  if (resume) args.push("--resume", resume);
  if (mcp) args.push("--mcp-config", mcp.path, "--strict-mcp-config");
  return {
    args,
    timeoutMs,
    cleanup: mcp ? mcp.cleanup : () => {},
    environment: {
      ...process.env,
      ANTHROPIC_API_KEY: apiKey,
      ANTHROPIC_BASE_URL: baseUrl,
      ANTHROPIC_MODEL: model,
      DISABLE_AUTOUPDATER: "1",
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
    },
  };
}

function parseClaudeOutput(stdout) {
  const text = String(stdout || "").trim();
  if (!text) return { reply: "", tool_events: [] };
  try {
    const value = JSON.parse(text);
    if (value && typeof value === "object") {
      const reply = typeof value.result === "string"
        ? value.result
        : typeof value.reply === "string"
          ? value.reply
          : typeof value.message === "string"
            ? value.message
            : text;
      return {
        reply,
        session_id: typeof value.session_id === "string" ? value.session_id : null,
        tool_events: Array.isArray(value.tool_events) ? value.tool_events : [],
      };
    }
  } catch (_) {
    // The CLI can still return a useful plain-text final response if a gateway
    // does not preserve Claude Code's JSON envelope.
  }
  return { reply: text, tool_events: [] };
}

function runClaude(body) {
  const { args, environment, timeoutMs, cleanup } = claudeInvocation(body, "json");

  return new Promise((resolve, reject) => {
    const id = runId(body.run_id);
    if (activeRuns.has(id)) {
      cleanup();
      reject(new Error(`duplicate run_id: ${id}`));
      return;
    }
    const child = spawn("claude", args, {
      cwd: WORKSPACE,
      env: environment,
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    });
    const record = { child, interrupted: false };
    activeRuns.set(id, record);
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      terminateChild(child, "SIGTERM");
      setTimeout(() => terminateChild(child, "SIGKILL"), 5000).unref();
    }, timeoutMs);

    child.stdout.on("data", (chunk) => { stdout = appendOutput(stdout, chunk); });
    child.stderr.on("data", (chunk) => { stderr = appendOutput(stderr, chunk); });
    child.on("error", (error) => {
      clearTimeout(timer);
      activeRuns.delete(id);
      cleanup();
      reject(new Error(`could not start Claude Code: ${error.message}`));
    });
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      activeRuns.delete(id);
      cleanup();
      if (record.interrupted) {
        reject(new Error("Claude Code interrupted by operator"));
        return;
      }
      if (timedOut) {
        reject(new Error(`Claude Code timed out after ${timeoutMs}ms`));
        return;
      }
      if (code !== 0) {
        const detail = stderr.trim().slice(-4000) || `exit=${code} signal=${signal || "none"}`;
        reject(new Error(`Claude Code failed: ${detail}`));
        return;
      }
      resolve({ ...parseClaudeOutput(stdout), run_id: id });
    });
  });
}

function collectToolEvents(event, toolEvents) {
  if (!event || event.type !== "assistant") return;
  const message = event.message && typeof event.message === "object" ? event.message : event;
  const content = Array.isArray(message.content) ? message.content : [];
  for (const block of content) {
    if (!block || block.type !== "tool_use" || typeof block.name !== "string") continue;
    if (!toolEvents.some((item) => item.name === block.name)) {
      toolEvents.push({ name: block.name });
    }
  }
}

function shouldForwardStreamEvent(event) {
  if (!event || typeof event !== "object") return false;
  if (event.type === "system" && ["status", "thinking_tokens"].includes(event.subtype)) {
    return false;
  }
  if (event.type === "stream_event") {
    const inner = event.event;
    const delta = inner && typeof inner === "object" ? inner.delta : null;
    if (delta && ["input_json_delta", "thinking_delta"].includes(delta.type)) return false;
  }
  return true;
}

function runClaudeStream(body, response) {
  const { args, environment, timeoutMs, cleanup } = claudeInvocation(body, "stream-json");
  return new Promise((resolve) => {
    const id = runId(body.run_id);
    if (activeRuns.has(id)) {
      cleanup();
      writeNdjson(response, { type: "error", error: `duplicate run_id: ${id}` });
      response.end();
      resolve();
      return;
    }
    const child = spawn("claude", args, {
      cwd: WORKSPACE,
      env: environment,
      stdio: ["ignore", "pipe", "pipe"],
      detached: true,
    });
    const record = { child, interrupted: false };
    activeRuns.set(id, record);
    let stdoutBuffer = "";
    let stderr = "";
    let outputBytes = 0;
    let timedOut = false;
    let ended = false;
    let finalEvent = null;
    let claudeSessionId = null;
    const toolEvents = [];
    const finish = (payload) => {
      if (ended) return;
      ended = true;
      activeRuns.delete(id);
      cleanup();
      writeNdjson(response, payload);
      response.end();
      resolve();
    };
    const handleLine = (line) => {
      const text = String(line || "").trim();
      if (!text) return;
      let event;
      try {
        event = JSON.parse(text);
      } catch (_) {
        writeNdjson(response, { type: "event", event: { type: "text", text: text.slice(0, 12000) } });
        return;
      }
      if (!event || typeof event !== "object") return;
      if (
        typeof event.session_id === "string"
        && /^[A-Za-z0-9_-]{8,128}$/.test(event.session_id)
        && event.session_id !== claudeSessionId
      ) {
        claudeSessionId = event.session_id;
        writeNdjson(response, { type: "claude_session", session_id: claudeSessionId });
      }
      collectToolEvents(event, toolEvents);
      if (event.type === "result") finalEvent = event;
      if (shouldForwardStreamEvent(event)) writeNdjson(response, { type: "event", event });
    };
    const timer = setTimeout(() => {
      timedOut = true;
      terminateChild(child, "SIGTERM");
      setTimeout(() => terminateChild(child, "SIGKILL"), 5000).unref();
    }, timeoutMs);

    writeNdjson(response, { type: "started", run_id: id, message: "Claude Code started" });
    child.stdout.on("data", (chunk) => {
      outputBytes += chunk.length;
      if (outputBytes > MAX_OUTPUT_BYTES) {
        terminateChild(child, "SIGTERM");
        return;
      }
      stdoutBuffer += chunk.toString("utf8");
      const lines = stdoutBuffer.split(/\r?\n/);
      stdoutBuffer = lines.pop() || "";
      for (const line of lines) handleLine(line);
    });
    child.stderr.on("data", (chunk) => {
      stderr = appendOutput(stderr, chunk);
      const text = chunk.toString("utf8").trim();
      if (text) writeNdjson(response, { type: "stderr", text: text.slice(-12000) });
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      finish({ type: "error", error: `could not start Claude Code: ${error.message}` });
    });
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      if (stdoutBuffer.trim()) handleLine(stdoutBuffer);
      if (record.interrupted) {
        finish({
          type: "interrupted",
          run_id: id,
          session_id: claudeSessionId,
          message: "IPC interrupted by operator",
        });
        return;
      }
      if (timedOut) {
        finish({ type: "error", error: `Claude Code timed out after ${timeoutMs}ms` });
        return;
      }
      if (code !== 0) {
        const detail = stderr.trim().slice(-4000) || `exit=${code} signal=${signal || "none"}`;
        finish({ type: "error", error: `Claude Code failed: ${detail}` });
        return;
      }
      finish({
        type: "result",
        run_id: id,
        reply: typeof finalEvent?.result === "string" ? finalEvent.result : "",
        session_id: typeof finalEvent?.session_id === "string" ? finalEvent.session_id : claudeSessionId,
        tool_events: toolEvents,
      });
    });
  });
}

const server = http.createServer(async (request, response) => {
  if (request.method === "GET" && request.url === "/health") {
    sendJson(response, 200, {
      ok: true,
      service: "ipc-claude-runner",
      cli: fs.existsSync("/usr/local/bin/claude"),
      workspace: WORKSPACE,
      active_runs: activeRuns.size,
    });
    return;
  }
  if (request.method === "POST" && request.url === "/runs/cancel") {
    if (!authorized(request)) {
      sendJson(response, 401, { error: "invalid runner token" });
      return;
    }
    let body;
    try {
      body = await readJson(request);
    } catch (error) {
      sendJson(response, 400, { error: error.message });
      return;
    }
    const id = typeof body.run_id === "string" ? body.run_id : "";
    if (!/^run_[A-Za-z0-9_-]{8,128}$/.test(id)) {
      sendJson(response, 400, { error: "a valid run_id is required" });
      return;
    }
    const result = requestCancellation(id);
    sendJson(response, result.ok ? 202 : 404, result);
    return;
  }
  if (request.method === "POST" && request.url === "/run/stream") {
    if (!authorized(request)) {
      sendJson(response, 401, { error: "invalid runner token" });
      return;
    }
    if (activeRuns.size >= MAX_CONCURRENT_RUNS) {
      sendJson(response, 429, { error: "Claude Code runner is busy" });
      return;
    }
    let body;
    try {
      body = await readJson(request);
    } catch (error) {
      sendJson(response, 400, { error: error.message });
      return;
    }
    response.writeHead(200, {
      "Content-Type": "application/x-ndjson; charset=utf-8",
      "Cache-Control": "no-cache, no-store",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    });
    try {
      await runClaudeStream(body, response);
    } catch (error) {
      writeNdjson(response, { type: "error", error: error.message });
      response.end();
    } finally {
      // runClaudeStream removes the corresponding child from activeRuns on
      // every close path; keeping the map here avoids cancellation races.
    }
    return;
  }
  if (request.method !== "POST" || request.url !== "/run") {
    sendJson(response, 404, { error: "not found" });
    return;
  }
  if (!authorized(request)) {
    sendJson(response, 401, { error: "invalid runner token" });
    return;
  }
  if (activeRuns.size >= MAX_CONCURRENT_RUNS) {
    sendJson(response, 429, { error: "Claude Code runner is busy" });
    return;
  }
  let body;
  try {
    body = await readJson(request);
  } catch (error) {
    sendJson(response, 400, { error: error.message });
    return;
  }
  try {
    const result = await runClaude(body);
    sendJson(response, 200, result);
  } catch (error) {
    sendJson(response, 502, { error: error.message });
  }
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`ipc-claude-runner listening on ${PORT}`);
});
