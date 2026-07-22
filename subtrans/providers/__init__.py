"""Provider registry.

`build()` turns resolved `Settings` into a live adapter. Adding a provider that speaks the
OpenAI chat-completions shape needs no code here at all — just an entry in
`config.PROVIDERS`.
"""

from __future__ import annotations

from ..config import ConfigError, Settings, resolve
from .base import Provider, ProviderError, RefusalError, TruncatedError, Usage

__all__ = [
    "Provider", "ProviderError", "RefusalError", "TruncatedError", "Usage", "build",
]


def build(settings: Settings | None = None, effort: str = "medium",
          max_tokens: int = 16000, **resolve_kwargs) -> Provider:
    """Instantiate the adapter for `settings` (resolving config if not supplied)."""
    if settings is None:
        settings = resolve(**resolve_kwargs)

    if settings.spec.kind == "anthropic":
        from .claude import ClaudeProvider

        return ClaudeProvider(settings, effort=effort, max_tokens=max_tokens)
    if settings.spec.kind == "openai":
        from .openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(settings, effort=effort, max_tokens=max_tokens)
    raise ConfigError(f"provider {settings.provider!r} has unknown kind {settings.spec.kind!r}")
