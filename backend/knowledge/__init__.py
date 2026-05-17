"""Local-LLM knowledge recall — multi-model fact extraction."""

from .local_recall import LocalKnowledgeService, DEFAULT_MODEL_CHAIN
from . import error_patterns

__all__ = ["LocalKnowledgeService", "DEFAULT_MODEL_CHAIN", "error_patterns"]
