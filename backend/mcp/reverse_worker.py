"""One-shot PyGhidra worker used by the reverse MCP.

JPype cannot safely stop a wedged JVM from another Python thread. Running each
analysis in a child process lets the MCP parent enforce a real wall-clock
deadline and clean up the temporary Ghidra project even after a forced kill.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def _remaining(deadline: float) -> int:
    return max(1, int(deadline - time.monotonic()))


def _run(
    operation: str,
    binary: str,
    timeout: int,
    *,
    function: str = "main",
    limit: int = 40,
    addr: str = "0",
    count: int = 40,
    min_len: int = 4,
    project_dir: str,
) -> dict[str, Any]:
    from backend.mcp import reverse_mcp

    deadline = time.monotonic() + max(1, timeout)
    reverse_mcp._ensure_jvm()
    import pyghidra

    with pyghidra.open_program(
        str(reverse_mcp._ensure_binary(binary)),
        project_location=project_dir,
        analyze=False,
    ) as flat:
        program = flat.getCurrentProgram()
        pyghidra.analyze(program, pyghidra.task_monitor(_remaining(deadline)))

        if operation == "decompile":
            selected = reverse_mcp._find_function(flat, program, function)
            if selected is None:
                raise LookupError(f"function not found: {function}")
            pseudocode = reverse_mcp._decompile_function(
                program, selected, _remaining(deadline)
            )
            return {
                "available": True,
                "binary": str(Path(binary).resolve()),
                "function": str(selected.getName()),
                "address": str(selected.getEntryPoint()),
                "pseudocode": pseudocode,
            }

        if operation == "decompile_all":
            results = []
            for selected in reverse_mcp._functions(program):
                if len(results) >= max(1, min(limit, 200)):
                    break
                if selected.isThunk() or selected.isExternal():
                    continue
                try:
                    pseudocode = reverse_mcp._decompile_function(
                        program, selected, _remaining(deadline)
                    )
                except Exception as exc:
                    results.append(
                        {
                            "name": str(selected.getName()),
                            "address": str(selected.getEntryPoint()),
                            "error": str(exc),
                        }
                    )
                    continue
                results.append(
                    {
                        "name": str(selected.getName()),
                        "address": str(selected.getEntryPoint()),
                        "pseudocode": pseudocode,
                    }
                )
            return {
                "available": True,
                "binary": str(Path(binary).resolve()),
                "functions": results,
            }

        if operation == "list_functions":
            functions = [
                {
                    "name": str(item.getName()),
                    "address": str(item.getEntryPoint()),
                    "size": int(item.getBody().getNumAddresses()),
                }
                for item in reverse_mcp._functions(program)
            ]
            return {
                "available": True,
                "binary": str(Path(binary).resolve()),
                "functions": functions,
            }

        if operation == "strings":
            from ghidra.program.util import DefinedDataIterator

            strings = []
            for data in reverse_mcp._java_iter(
                DefinedDataIterator.definedStrings(program)
            ):
                value = str(data.getValue())
                if len(value) >= max(1, min_len):
                    strings.append(
                        {"address": str(data.getAddress()), "value": value}
                    )
            return {
                "available": True,
                "binary": str(Path(binary).resolve()),
                "strings": strings,
            }

        if operation == "disassemble":
            address = flat.toAddr(int(addr, 0))
            iterator = program.getListing().getInstructions(address, True)
            instructions = []
            for instruction in reverse_mcp._java_iter(iterator):
                if len(instructions) >= max(1, min(count, 500)):
                    break
                instructions.append(
                    {
                        "address": str(instruction.getAddress()),
                        "instruction": str(instruction),
                    }
                )
            return {
                "available": True,
                "binary": str(Path(binary).resolve()),
                "instructions": instructions,
            }

    raise ValueError(f"unknown reverse worker operation: {operation}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation")
    parser.add_argument("--binary", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--function", default="main")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--addr", default="0")
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--min-len", type=int, default=4)
    args = parser.parse_args(argv)

    output = Path(args.output)
    try:
        result = _run(
            args.operation,
            args.binary,
            args.timeout,
            function=args.function,
            limit=args.limit,
            addr=args.addr,
            count=args.count,
            min_len=args.min_len,
            project_dir=args.project_dir,
        )
    except Exception as exc:
        result = {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        return 1
    output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
