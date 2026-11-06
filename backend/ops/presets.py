"""Built-in platform workflow registry; legacy imports remain supported.

The registry itself lives with the platform packages:
`backend.platform.presets`.  This module is the stable import path the ops
layer and tests have always used.
"""
from __future__ import annotations

from backend.platform.presets import (
    DEFAULT_WORKFLOW_TEMPLATE,
    GZCTF_TEMPLATE_VERSION,
    ICHUNQIU_TEMPLATE_VERSION,
    NESTCTF_TEMPLATE_VERSION,
    RET2SHELL_TEMPLATE_VERSION,
    TSECBENCH_TEMPLATE_VERSION,
    GZCTFWorkflowInput,
    IchunqiuWorkflowInput,
    NestctfWorkflowInput,
    Ret2ShellWorkflowInput,
    TSECBenchWorkflowInput,
    allowed_secret_names,
    configure_workflow,
    deployment_presets,
    is_known_platform,
    is_preset_template,
    preset_input_model,
    preset_instructions,
    preset_mcp_tools,
    preset_secret_names,
    preset_setup_prompt,
    preset_tool_definitions,
    preset_tool_names,
    runtime_mcp_note,
    template_for_tool,
    validate_platform_mapping,
    workflow_preset,
    workflow_templates,
)

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
