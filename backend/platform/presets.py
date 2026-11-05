"""Built-in platform presets and the hooks the shared layers use.

Each platform package owns its workflow details in
`backend/platform/<name>/workflow.py`:

    TEMPLATE               preset registry entry (metadata + IPC setup contract)
    PLATFORM               platform id used by FieldMapping/ChallengeMappingSpec
    WORKFLOW_INPUT         pydantic input model for the preset
    TOOL_NAME              IPC tool name, for example ret2shell_workflow
    TOOL_DEFINITION        JSON schema of that IPC tool
    MCP_TOOL_DESCRIPTION   description of the matching ipc_* MCP tool
    ALLOWED_SECRET_NAMES   native credentials accepted in the secret store
    validate_mapping()     platform-specific list_url/game_id rules
    configure()            create or update the workflow through the ops service
    build_deployment()     optional: deployment client + Member MCP from env

Shared modules (ops service, IPC tools, API routes, AppState, mapping) must not
branch on a platform name; they ask this registry instead.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from backend.platform.gzctf import workflow as gzctf
from backend.platform.ichunqiu import workflow as ichunqiu
from backend.platform.nestctf import workflow as nestctf
from backend.platform.ret2shell import workflow as ret2shell
from backend.platform.tsecbench import workflow as tsecbench

DEFAULT_WORKFLOW_TEMPLATE = "gzctf"

_MODULES = (gzctf, tsecbench, ret2shell, ichunqiu, nestctf)
_BY_TEMPLATE = {str(module.TEMPLATE["id"]): module for module in _MODULES}
_BY_PLATFORM = {str(module.PLATFORM): module for module in _MODULES}
_BY_TOOL = {str(module.TOOL_NAME): module for module in _MODULES}


def workflow_templates() -> list[dict]:
    return deepcopy([module.TEMPLATE for module in _MODULES])


def workflow_preset(template_id: str) -> dict | None:
    module = _BY_TEMPLATE.get(str(template_id))
    return deepcopy(module.TEMPLATE) if module is not None else None


def is_preset_template(template_id: str) -> bool:
    return str(template_id) in _BY_TEMPLATE


def is_known_platform(platform: str) -> bool:
    # "local" is a run-owned single-challenge adapter, not a remote preset: it
    # never appears in workflow_templates() or preset_tool_definitions().
    return str(platform) in {"http_json", "local"} or str(platform) in _BY_PLATFORM


def preset_setup_prompt(template_id: str) -> str:
    module = _BY_TEMPLATE[str(template_id)]
    return str((module.TEMPLATE.get("ipc_setup") or {}).get("prompt") or "")


def preset_input_model(template_id: str) -> Any:
    return _BY_TEMPLATE[str(template_id)].WORKFLOW_INPUT


def preset_tool_definitions() -> list[dict]:
    return deepcopy([module.TOOL_DEFINITION for module in _MODULES])


def preset_tool_names() -> frozenset[str]:
    return frozenset(_BY_TOOL)


def template_for_tool(tool_name: str) -> str | None:
    module = _BY_TOOL.get(str(tool_name))
    return str(module.TEMPLATE["id"]) if module is not None else None


def preset_mcp_tools() -> list[dict[str, str]]:
    return [
        {
            "name": f"ipc_{module.TOOL_NAME}",
            "description": str(module.MCP_TOOL_DESCRIPTION),
            "template_id": str(module.TEMPLATE["id"]),
        }
        for module in _MODULES
    ]


def preset_secret_names() -> list[str]:
    names: set[str] = set()
    for module in _MODULES:
        names.update(str(name) for name in module.TEMPLATE.get("secret_names", ()))
    return sorted(names)


def configure_workflow(
    template_id: str,
    service: Any,
    data: dict[str, Any],
    *,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    module = _BY_TEMPLATE.get(str(template_id))
    if module is None:
        raise KeyError(template_id)
    return module.configure(service, data, workflow_id=workflow_id)


def allowed_secret_names(platform: str) -> set[str]:
    module = _BY_PLATFORM.get(str(platform))
    if module is None:
        return set()
    return set(module.ALLOWED_SECRET_NAMES)


def validate_platform_mapping(mapping: Any) -> None:
    module = _BY_PLATFORM.get(str(mapping.platform))
    if module is not None:
        module.validate_mapping(mapping)


def deployment_presets(environ: Any = None) -> list[dict[str, Any]]:
    """Return deployment-level clients/MCPs that the environment configured."""

    built: list[dict[str, Any]] = []
    for module in _MODULES:
        hook = getattr(module, "build_deployment", None)
        if hook is None:
            continue
        item = hook(environ)
        if item:
            built.append(item)
    return built


def preset_instructions() -> str:
    tools = ", ".join(
        f"{module.TOOL_NAME} for {module.TEMPLATE['name']}" for module in _MODULES
    )
    secrets = ", ".join(preset_secret_names())
    return (
        "For a built-in platform, use the preset tool instead of "
        "propose_platform_workflow: "
        + tools
        + ". These tools already own the native adapter and need only the "
        "operator's personalization values. Ask for missing non-secret fields "
        "with the question tool, one field at a time, and create the draft as "
        "soon as the required identity fields are known. Never put participant "
        "cookies, tokens, or passwords in the workflow, tool settings, or "
        "ordinary chat. After the draft exists, ask for credentials with "
        "question, the workflow id and the matching secret_name ("
        + secrets
        + "). The answer is stored in the workflow secret store and the native "
        "adapter performs the authenticated platform requests."
    )


def runtime_mcp_note(mcp_names: Any) -> str:
    names = set(mcp_names or ())
    for module in _MODULES:
        mcp_name = getattr(module, "MCP_NAME", "")
        if mcp_name and mcp_name in names:
            return str(getattr(module, "RUNTIME_MCP_NOTE", ""))
    return ""


GZCTFWorkflowInput = gzctf.WORKFLOW_INPUT
TSECBenchWorkflowInput = tsecbench.WORKFLOW_INPUT
Ret2ShellWorkflowInput = ret2shell.WORKFLOW_INPUT
IchunqiuWorkflowInput = ichunqiu.WORKFLOW_INPUT
NestctfWorkflowInput = nestctf.WORKFLOW_INPUT
GZCTF_TEMPLATE_VERSION = gzctf.GZCTF_TEMPLATE_VERSION
TSECBENCH_TEMPLATE_VERSION = tsecbench.TSECBENCH_TEMPLATE_VERSION
RET2SHELL_TEMPLATE_VERSION = ret2shell.RET2SHELL_TEMPLATE_VERSION
ICHUNQIU_TEMPLATE_VERSION = ichunqiu.ICHUNQIU_TEMPLATE_VERSION
NESTCTF_TEMPLATE_VERSION = nestctf.NESTCTF_TEMPLATE_VERSION


__all__ = [
    "DEFAULT_WORKFLOW_TEMPLATE",
    "GZCTF_TEMPLATE_VERSION",
    "ICHUNQIU_TEMPLATE_VERSION",
    "NESTCTF_TEMPLATE_VERSION",
    "RET2SHELL_TEMPLATE_VERSION",
    "TSECBENCH_TEMPLATE_VERSION",
    "GZCTFWorkflowInput",
    "IchunqiuWorkflowInput",
    "NestctfWorkflowInput",
    "Ret2ShellWorkflowInput",
    "TSECBenchWorkflowInput",
    "allowed_secret_names",
    "configure_workflow",
    "deployment_presets",
    "is_known_platform",
    "is_preset_template",
    "preset_input_model",
    "preset_instructions",
    "preset_mcp_tools",
    "preset_secret_names",
    "preset_setup_prompt",
    "preset_tool_definitions",
    "preset_tool_names",
    "runtime_mcp_note",
    "template_for_tool",
    "validate_platform_mapping",
    "workflow_preset",
    "workflow_templates",
]
