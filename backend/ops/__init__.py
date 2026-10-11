from backend.ops.models import PlatformWorkflowSpec

__all__ = ["OpsAgentService", "PlatformWorkflowSpec"]


def __getattr__(name):
    if name == "OpsAgentService":
        from backend.ops.service import OpsAgentService
        return OpsAgentService
    raise AttributeError(name)
