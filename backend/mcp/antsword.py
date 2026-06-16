from __future__ import annotations

import base64
import urllib.parse
from uuid import uuid4

from backend.mcp.base import MCPServer


def build_antsword_mcp() -> MCPServer:
    server = MCPServer(name="antsword", description="WebShell workflow helpers")

    @server.tool(
        name="encoder",
        description="Encode a payload for transport.",
        input_schema={
            "type": "object",
            "properties": {
                "data": {"type": "string"},
                "scheme": {"type": "string", "enum": ["base64", "url", "hex"]},
            },
            "required": ["data"],
        },
    )
    def encoder(data: str, scheme: str = "base64"):
        if scheme == "url":
            encoded = urllib.parse.quote(data)
        elif scheme == "hex":
            encoded = data.encode().hex()
        else:
            encoded = base64.b64encode(data.encode()).decode()
        return {"scheme": scheme, "encoded": encoded}

    @server.tool(
        name="webshell_generator",
        description="Describe a webshell template without returning executable code.",
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["php", "jsp", "aspx"]},
                "password": {"type": "string"},
            },
            "required": ["kind", "password"],
        },
    )
    def webshell_generator(kind: str = "php", password: str = "cmd"):
        return {
            "kind": kind,
            "password": password,
            "shell": f"SAFE_TEMPLATE[{kind}]: command handler keyed by '{password}'",
            "safe_stub": True,
        }

    @server.tool(
        name="upload",
        description="Prepare a multipart/form-data upload body shape.",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "filename": {"type": "string"},
                "field": {"type": "string"},
            },
            "required": ["content", "filename"],
        },
    )
    def upload(content: str, filename: str, field: str = "file"):
        boundary = f"----ipc{uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            f"{content}\r\n"
            f"--{boundary}--\r\n"
        )
        return {"headers": {"Content-Type": f"multipart/form-data; boundary={boundary}"}, "body": body}

    @server.tool(
        name="php_bypass",
        description="Return high-level PHP bypass technique metadata.",
        input_schema={
            "type": "object",
            "properties": {"technique": {"type": "string"}},
            "required": ["technique"],
        },
    )
    def php_bypass(technique: str):
        notes = {
            "disable_functions_FFI": "FFI-based disable_functions bypass technique selected; executable code is omitted by the safe stub.",
            "assert": "Dynamic assertion technique selected; executable code is omitted by the safe stub.",
            "preg_replace_e": "Legacy replacement modifier technique selected; executable code is omitted by the safe stub.",
        }
        return {"technique": technique, "snippet": notes.get(technique, "Technique metadata unavailable.")}

    @server.tool(
        name="traffic_mutation",
        description="Mutate a payload to bypass simple WAF signatures.",
        input_schema={
            "type": "object",
            "properties": {
                "payload": {"type": "string"},
                "method": {"type": "string", "enum": ["comment_insert", "urlencode"]},
            },
            "required": ["payload"],
        },
    )
    def traffic_mutation(payload: str, method: str = "comment_insert"):
        if method == "urlencode":
            mutated = urllib.parse.quote(payload)
        elif len(payload) > 1:
            mutated = "/**/".join(payload.split(" "))
            if mutated == payload:
                mutated = payload[:1] + "/**/" + payload[1:]
        else:
            mutated = payload
        return {"method": method, "mutated": mutated}

    return server
