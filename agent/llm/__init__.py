# Picks the provider from configuration and hands back something implementing LLMProvider.
# Exists as the single place the rest of the agent learns which vendor is in use - the loop, the
# eval and the grader all call build_provider() and never import a vendor SDK themselves.
# Adding a provider is a new adapter module plus one line in _PROVIDERS.

from __future__ import annotations

from .base import (
    AssistantTurn, AssistantTurnRef, LLMProvider, ToolCall, ToolResult, ToolResultsTurn,
    ToolSpec, UserTurn,
)


def _groq(cfg, http_client):
    from .openai_compat import groq_provider
    return groq_provider(model=cfg.MODEL, http_client=http_client)


def _openai_compatible(cfg, http_client):
    from .openai_compat import OpenAICompatProvider
    return OpenAICompatProvider(
        model=cfg.MODEL, base_url=cfg.LLM_BASE_URL, api_key_env=cfg.LLM_API_KEY_ENV,
        name="openai-compatible", http_client=http_client,
    )


def _anthropic(cfg, http_client):
    from .anthropic_adapter import AnthropicProvider
    return AnthropicProvider(
        model=cfg.MODEL, http_client=http_client,
        thinking=getattr(cfg, "THINKING", None), effort=getattr(cfg, "EFFORT", None),
    )


_PROVIDERS = {
    "groq": _groq,
    "anthropic": _anthropic,
    "openai-compatible": _openai_compatible,
}


def build_provider(cfg=None, http_client=None):
    if cfg is None:
        from .. import config as cfg
    try:
        factory = _PROVIDERS[cfg.LLM_PROVIDER]
    except KeyError:
        raise SystemExit(
            f"Unknown LLM_PROVIDER '{cfg.LLM_PROVIDER}'. "
            f"Known: {', '.join(sorted(_PROVIDERS))}."
        )
    return factory(cfg, http_client)


__all__ = [
    "build_provider", "LLMProvider", "ToolSpec", "ToolCall", "ToolResult",
    "AssistantTurn", "UserTurn", "AssistantTurnRef", "ToolResultsTurn",
]
