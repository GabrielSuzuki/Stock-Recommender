"""The Stage B agents. Each one has a hard JSON contract and a offline stub."""
from .base import AgentError, JsonAgent, OfflineModel
from .catalyst import analyse_catalysts
from .editor import compose_brief
from .thesis import write_theses

__all__ = ["JsonAgent", "OfflineModel", "AgentError",
           "analyse_catalysts", "write_theses", "compose_brief"]
