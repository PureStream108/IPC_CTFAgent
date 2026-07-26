from __future__ import annotations

import asyncio
import atexit
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator

from backend.mcp.mcp_server import MCPServer, create_mcp_server
from backend.mcp.shared import _tool_unavailable

_JVM_STARTED = False
_PROJECTS: dict[str, tuple[Any, Any, Any]] = {}
_GHIDRA_LOCK = threading.RLock()
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


def _open_program(binary: str) -> tuple[Any, Any, Any]:
    path = str(_ensure_binary(binary))
    with _GHIDRA_LOCK:
        cached = _PROJECTS.get(path)
        if cached is not None:
            return cached
        _ensure_jvm()
        import pyghidra

        project_location = Path("/workspace/ghidra-projects")
        project_location.mkdir(parents=True, exist_ok=True)
        context = pyghidra.open_program(
            path,
            project_location=str(project_location),
            analyze=True,
        )
        flat = context.__enter__()
        program = flat.getCurrentProgram()
        opened = (context, flat, program)
        _PROJECTS[path] = opened
        return opened


def _close_projects() -> None:
    with _GHIDRA_LOCK:
        projects = list(_PROJECTS.values())
        _PROJECTS.clear()
    for context, _, _ in projects:
        try:
            context.__exit__(None, None, None)
        except Exception:
            pass


atexit.register(_close_projects)


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
        return str(decompiled.getC())
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


def _decompile_sync(binary: str, function: str, timeout: int) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
    except OSError as exc:
        return _tool_unavailable("reverse.decompile", str(exc), binary=binary, function=function)
    try:
        with _GHIDRA_LOCK:
            _, flat, program = _open_program(str(path))
            selected = _find_function(flat, program, function)
            if selected is None:
                raise LookupError(f"function not found: {function}")
            pseudocode = _decompile_function(program, selected, timeout)
            return {
                "available": True,
                "binary": str(path),
                "function": str(selected.getName()),
                "address": str(selected.getEntryPoint()),
                "pseudocode": pseudocode,
            }
    except Exception as exc:
        fallback = _r2_cmd_sync(str(path), f"aaa; pdf @ {function}")
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
        with _GHIDRA_LOCK:
            _, _, program = _open_program(str(path))
            results = []
            for function in _functions(program):
                if len(results) >= max(1, min(limit, 200)):
                    break
                if function.isThunk() or function.isExternal():
                    continue
                try:
                    pseudocode = _decompile_function(program, function, timeout)
                except Exception as exc:
                    results.append(
                        {
                            "name": str(function.getName()),
                            "address": str(function.getEntryPoint()),
                            "error": str(exc),
                        }
                    )
                    continue
                results.append(
                    {
                        "name": str(function.getName()),
                        "address": str(function.getEntryPoint()),
                        "pseudocode": pseudocode,
                    }
                )
            return {"available": True, "binary": str(path), "functions": results}
    except Exception as exc:
        return _tool_unavailable("reverse.decompile_all", str(exc), binary=binary)


def _list_functions_sync(binary: str) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
        with _GHIDRA_LOCK:
            _, _, program = _open_program(str(path))
            functions = [
                {
                    "name": str(function.getName()),
                    "address": str(function.getEntryPoint()),
                    "size": int(function.getBody().getNumAddresses()),
                }
                for function in _functions(program)
            ]
        return {"available": True, "binary": str(path), "functions": functions}
    except Exception as exc:
        return _tool_unavailable("reverse.list_functions", str(exc), binary=binary)


def _strings_sync(binary: str, min_len: int) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
        with _GHIDRA_LOCK:
            _, _, program = _open_program(str(path))
            from ghidra.program.util import DefinedDataIterator

            strings = []
            for data in _java_iter(DefinedDataIterator.definedStrings(program)):
                value = str(data.getValue())
                if len(value) >= max(1, min_len):
                    strings.append({"address": str(data.getAddress()), "value": value})
        return {"available": True, "binary": str(path), "strings": strings}
    except Exception as exc:
        return _tool_unavailable("reverse.strings", str(exc), binary=binary)


def _disassemble_sync(binary: str, addr: str, count: int) -> dict[str, Any]:
    try:
        path = _ensure_binary(binary)
        with _GHIDRA_LOCK:
            _, flat, program = _open_program(str(path))
            address = flat.toAddr(int(addr, 0))
            iterator = program.getListing().getInstructions(address, True)
            instructions = []
            for instruction in _java_iter(iterator):
                if len(instructions) >= max(1, min(count, 500)):
                    break
                instructions.append(
                    {"address": str(instruction.getAddress()), "instruction": str(instruction)}
                )
        return {"available": True, "binary": str(path), "instructions": instructions}
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
    async def list_functions(binary: str) -> dict[str, Any]:
        return await asyncio.to_thread(_list_functions_sync, binary)

    @server.tool(name="strings", description="List Ghidra-defined strings with addresses.")
    async def strings(binary: str, min_len: int = 4) -> dict[str, Any]:
        return await asyncio.to_thread(_strings_sync, binary, min_len)

    @server.tool(name="disassemble", description="Disassemble instructions starting at an address.")
    async def disassemble(binary: str, addr: str, count: int = 40) -> dict[str, Any]:
        return await asyncio.to_thread(_disassemble_sync, binary, addr, count)

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
