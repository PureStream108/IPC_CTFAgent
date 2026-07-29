from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator

from backend.mcp.mcp_server import MCPServer, create_mcp_server
from backend.mcp.shared import _tool_unavailable

_JVM_STARTED = False
_DEFAULT_GHIDRA_INSTALL_DIR = Path("/opt/ghidra")
_DEFAULT_GHIDRA_JAVA_HOME = Path("/opt/java21")


def _ensure_binary(binary: str) -> Path:
    path = Path(binary).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"binary not found: {binary}")
    return path


def _ensure_jvm() -> None:
    global _JVM_STARTED
    if _JVM_STARTED:
        return
    import pyghidra

    install_dir_value = os.environ.get("GHIDRA_INSTALL_DIR")
    install_dir = Path(install_dir_value) if install_dir_value else _DEFAULT_GHIDRA_INSTALL_DIR
    start_kwargs: dict[str, Any] = {}
    if install_dir_value or install_dir.is_dir():
        start_kwargs["install_dir"] = install_dir

    java_home_value = os.environ.get("GHIDRA_JAVA_HOME")
    if not java_home_value and _DEFAULT_GHIDRA_JAVA_HOME.is_dir():
        java_home_value = str(_DEFAULT_GHIDRA_JAVA_HOME)
        os.environ["GHIDRA_JAVA_HOME"] = java_home_value
    if java_home_value:
        os.environ.setdefault("JAVA_HOME", java_home_value)
        java_bin = str(Path(java_home_value) / "bin")
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if java_bin not in path_entries:
            os.environ["PATH"] = os.pathsep.join([java_bin, *path_entries])

    pyghidra.start(verbose=False, **start_kwargs)
    _JVM_STARTED = True


def _java_iter(iterator: Any) -> Iterator[Any]:
    if hasattr(iterator, "hasNext"):
        while iterator.hasNext():
            yield iterator.next()
        return
    yield from iterator


def _functions(program: Any) -> list[Any]:
    manager = program.getFunctionManager()
    return list(_java_iter(manager.getFunctions(True)))


def _find_function(flat: Any, program: Any, selector: str) -> Any | None:
    manager = program.getFunctionManager()
    text = selector.strip()
    if text.lower().startswith("0x"):
        address = flat.toAddr(int(text, 16))
        return manager.getFunctionAt(address) or manager.getFunctionContaining(address)

    exact = flat.getGlobalFunctions(text)
    if exact:
        return exact[0]
    lowered = text.casefold()
    return next(
        (function for function in _functions(program) if lowered in str(function.getName()).casefold()),
        None,
    )


def _decompile_function(program: Any, function: Any, timeout: int) -> str:
    from ghidra.app.decompiler import DecompInterface
    from ghidra.util.task import ConsoleTaskMonitor

    decompiler = DecompInterface()
    try:
        if not decompiler.openProgram(program):
            raise RuntimeError("Ghidra decompiler could not open the program")
        result = decompiler.decompileFunction(function, timeout, ConsoleTaskMonitor())
        if not result.decompileCompleted():
            raise RuntimeError(str(result.getErrorMessage() or "Ghidra decompilation failed"))
        decompiled = result.getDecompiledFunction()
        if decompiled is None:
            raise RuntimeError("Ghidra returned no decompiled function")
        source = str(decompiled.getC())
        if not source.strip():
            raise RuntimeError("Ghidra returned empty pseudocode")
        return source
    finally:
        decompiler.dispose()


def _r2_cmd_sync(binary: str, cmd: str) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
        import r2pipe

        handle = r2pipe.open(str(path), flags=["-2"])
        try:
            output = handle.cmd(cmd)
        finally:
            handle.quit()
        return {"available": True, "binary": str(path), "command": cmd, "output": output}
    except Exception as exc:
        return _tool_unavailable("reverse.r2_cmd", str(exc), binary=binary, command=cmd)


def _r2_decompile_sync(binary: str, function: str) -> dict[str, Any]:
    """Resolve a function safely and return non-empty r2 disassembly."""
    try:
        path = _ensure_binary(binary)
        import r2pipe

        handle = r2pipe.open(str(path), flags=["-2"])
        try:
            handle.cmd("aaa")
            functions = handle.cmdj("aflj") or []
            selector = function.strip()
            offset: int | None = None
            try:
                offset = int(selector, 0)
            except ValueError:
                pass
            if offset is None:
                exact = [
                    item
                    for item in functions
                    if str(item.get("name", "")) == selector
                ]
                prefixes = ("sym.", "dbg.", "fcn.")
                suffix = [
                    item
                    for item in functions
                    if any(
                        str(item.get("name", "")).startswith(prefix)
                        and str(item.get("name", ""))[len(prefix) :] == selector
                        for prefix in prefixes
                    )
                ]
                selected = (exact or suffix or [None])[0]
                if selected is None or selected.get("offset") is None:
                    raise LookupError(f"r2 function not found: {function}")
                raw_offset = selected["offset"]
                offset = (
                    int(raw_offset, 0)
                    if isinstance(raw_offset, str)
                    else int(raw_offset)
                )
            output = handle.cmd(f"pdf @ {offset}")
        finally:
            handle.quit()
        if not output or not output.strip():
            raise RuntimeError(f"r2 returned empty disassembly for {function}")
        return {
            "available": True,
            "binary": str(path),
            "function": function,
            "address": offset,
            "output": output,
        }
    except Exception as exc:
        return _tool_unavailable(
            "reverse.decompile.r2",
            str(exc),
            binary=binary,
            function=function,
        )


def _run_ghidra_worker(
    operation: str,
    binary: str,
    timeout: int,
    **arguments: Any,
) -> dict[str, Any]:
    path = _ensure_binary(binary)
    timeout = max(1, min(int(timeout), 600))
    with tempfile.TemporaryDirectory(prefix="ipc-ghidra-") as temp:
        temp_path = Path(temp)
        output = temp_path / "result.json"
        project_dir = temp_path / "project"
        project_dir.mkdir()
        command = [
            sys.executable,
            "-m",
            "backend.mcp.reverse_worker",
            operation,
            "--binary",
            str(path),
            "--output",
            str(output),
            "--project-dir",
            str(project_dir),
            "--timeout",
            str(timeout),
        ]
        for key, value in arguments.items():
            command.extend([f"--{key.replace('_', '-')}", str(value)])
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"PyGhidra {operation} exceeded {timeout}s hard timeout"
            ) from exc
        if output.exists():
            result = json.loads(output.read_text(encoding="utf-8"))
            if result.get("available"):
                return result
            raise RuntimeError(result.get("error", f"PyGhidra {operation} failed"))
        detail = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
            if part and part.strip()
        )
        raise RuntimeError(detail or f"PyGhidra worker exited {completed.returncode}")


def _decompile_sync(binary: str, function: str, timeout: int) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
    except OSError as exc:
        return _tool_unavailable("reverse.decompile", str(exc), binary=binary, function=function)
    try:
        return _run_ghidra_worker(
            "decompile",
            str(path),
            timeout,
            function=function,
        )
    except Exception as exc:
        fallback = _r2_decompile_sync(str(path), function)
        if fallback.get("available"):
            return {
                "available": True,
                "binary": str(path),
                "function": function,
                "pseudocode": None,
                "fallback": "r2",
                "fallback_reason": str(exc),
                "disassembly": fallback.get("output", ""),
            }
        return _tool_unavailable(
            "reverse.decompile",
            f"PyGhidra failed: {exc}; r2 fallback failed: {fallback.get('error', 'unknown error')}",
            binary=str(path),
            function=function,
        )


def _decompile_all_sync(binary: str, limit: int, timeout: int) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
        return _run_ghidra_worker(
            "decompile_all",
            str(path),
            timeout,
            limit=limit,
        )
    except Exception as exc:
        return _tool_unavailable("reverse.decompile_all", str(exc), binary=binary)


def _list_functions_sync(binary: str, timeout: int = 90) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
        return _run_ghidra_worker("list_functions", str(path), timeout)
    except Exception as exc:
        return _tool_unavailable("reverse.list_functions", str(exc), binary=binary)


def _strings_sync(binary: str, min_len: int, timeout: int = 90) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
        return _run_ghidra_worker(
            "strings", str(path), timeout, min_len=min_len
        )
    except Exception as exc:
        return _tool_unavailable("reverse.strings", str(exc), binary=binary)


def _disassemble_sync(
    binary: str, addr: str, count: int, timeout: int = 90
) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
        return _run_ghidra_worker(
            "disassemble",
            str(path),
            timeout,
            addr=addr,
            count=count,
        )
    except Exception as exc:
        return _tool_unavailable("reverse.disassemble", str(exc), binary=binary, address=addr)


def _run_command(command: list[str], timeout: int = 60) -> tuple[bool, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return result.returncode == 0, output


def _checksec_sync(binary: str) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
    except OSError as exc:
        return _tool_unavailable("reverse.checksec", str(exc), binary=binary)
    ok, output = _run_command(["checksec", f"--file={path}"])
    if not ok:
        return _tool_unavailable("reverse.checksec", output, binary=str(path))
    return {"available": True, "binary": str(path), "output": output}


def _file_info_sync(binary: str) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
    except OSError as exc:
        return _tool_unavailable("reverse.file_info", str(exc), binary=binary)
    file_ok, file_output = _run_command(["file", str(path)])
    readelf_ok, readelf_output = _run_command(["readelf", "-h", str(path)])
    if not file_ok and not readelf_ok:
        return _tool_unavailable(
            "reverse.file_info",
            "\n".join(part for part in (file_output, readelf_output) if part),
            binary=str(path),
        )
    return {
        "available": True,
        "binary": str(path),
        "file": file_output,
        "elf_header": readelf_output,
    }


def build_reverse_mcp() -> MCPServer:
    server = create_mcp_server("reverse", "PyGhidra decompilation and radare2 analysis")

    @server.tool(name="decompile", description="Decompile a named function or address to C with PyGhidra.")
    async def decompile(binary: str, function: str = "main", timeout: int = 90) -> dict[str, Any]:
        return await asyncio.to_thread(_decompile_sync, binary, function, timeout)

    @server.tool(name="decompile_all", description="Decompile non-external functions in a binary.")
    async def decompile_all(binary: str, limit: int = 40, timeout: int = 30) -> dict[str, Any]:
        return await asyncio.to_thread(_decompile_all_sync, binary, limit, timeout)

    @server.tool(name="list_functions", description="List Ghidra functions with addresses and sizes.")
    async def list_functions(binary: str, timeout: int = 90) -> dict[str, Any]:
        return await asyncio.to_thread(_list_functions_sync, binary, timeout)

    @server.tool(name="strings", description="List Ghidra-defined strings with addresses.")
    async def strings(binary: str, min_len: int = 4, timeout: int = 90) -> dict[str, Any]:
        return await asyncio.to_thread(_strings_sync, binary, min_len, timeout)

    @server.tool(name="disassemble", description="Disassemble instructions starting at an address.")
    async def disassemble(
        binary: str,
        addr: str,
        count: int = 40,
        timeout: int = 90,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            _disassemble_sync, binary, addr, count, timeout
        )

    @server.tool(name="r2_cmd", description="Run an r2 command against a binary.")
    async def r2_cmd(binary: str, cmd: str) -> dict[str, Any]:
        return await asyncio.to_thread(_r2_cmd_sync, binary, cmd)

    @server.tool(name="checksec", description="Report executable protection mechanisms.")
    async def checksec(binary: str) -> dict[str, Any]:
        return await asyncio.to_thread(_checksec_sync, binary)

    @server.tool(name="file_info", description="Return file(1) and ELF header information.")
    async def file_info(binary: str) -> dict[str, Any]:
        return await asyncio.to_thread(_file_info_sync, binary)

    return server
