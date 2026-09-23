from backend.ops.models import PlatformWorkflowSpec

__all__ = ["OpsAgentService", "PlatformWorkflowSpec"]


def __getattr__(name):
    # Schema/adapter consumers do not need the complete application runtime.
    if name == "OpsAgentService":
        from backend.ops.service import OpsAgentService
        return OpsAgentService
    raise AttributeError(name)
