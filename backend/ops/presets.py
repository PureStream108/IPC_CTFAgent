"""Built-in platform workflow registry; legacy imports remain supported."""
from copy import deepcopy

from backend.platform.gzctf.workflow import GZCTF_TEMPLATE_VERSION, GZCTFWorkflowInput
from backend.platform.gzctf.workflow import TEMPLATE as GZCTF_TEMPLATE
from backend.platform.tsecbench.workflow import TEMPLATE as TSECBENCH_TEMPLATE
from backend.platform.tsecbench.workflow import (
    TSECBENCH_TEMPLATE_VERSION,
    TSECBenchWorkflowInput,
)

DEFAULT_WORKFLOW_TEMPLATE = "gzctf"


def workflow_templates() -> list[dict]:
    return deepcopy([GZCTF_TEMPLATE, TSECBENCH_TEMPLATE])


__all__ = [
    "DEFAULT_WORKFLOW_TEMPLATE",
    "GZCTF_TEMPLATE_VERSION",
    "TSECBENCH_TEMPLATE_VERSION",
    "GZCTFWorkflowInput",
    "TSECBenchWorkflowInput",
    "workflow_templates",
]
