"""The provider seam.

Everything above this line — batching, the rolling context window, retry-then-split
recovery — lives in `engine.py` and is written once. A provider only has to turn one
(system, user, schema) triple into a parsed dict.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Provider", "Usage", "ProviderError", "RefusalError", "TruncatedError"]


class ProviderError(RuntimeError):
    """The request failed in a way worth retrying or reporting."""


class RefusalError(ProviderError):
    """The model declined the content outright. Retrying the same batch will not help."""


class TruncatedError(ProviderError):
    """The reply hit the output token ceiling — the batch is too large."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    requests: int = 0
    retries: int = 0  # batches re-requested after an id-contract failure
    splits: int = 0  # batches halved as a last resort

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.requests += other.requests

    def summary(self) -> str:
        parts = [
            f"{self.requests} requests",
            f"{self.input_tokens} in / {self.output_tokens} out",
        ]
        if self.cache_read:
            parts.append(f"{self.cache_read} cached")
        if self.retries or self.splits:
            # Surfaced deliberately: on a weaker model this is the number that tells you
            # whether the output can be trusted, and it is invisible in a read-through.
            parts.append(f"{self.retries} retries, {self.splits} splits")
        return ", ".join(parts)


class Provider:
    """Adapter interface. Subclasses implement `complete`."""

    name: str = ""
    label: str = ""
    model: str = ""
    supports_batch_api: bool = False

    def complete(self, system: str, user: str, schema: dict) -> tuple[dict, Usage]:
        """Return the model's JSON reply parsed against `schema`, plus token usage."""
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError(f"{self.name} does not support listing models")

    def __str__(self) -> str:
        return f"{self.label} ({self.model})"
