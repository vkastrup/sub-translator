"""Anthropic adapter.

Uses the official SDK rather than the OpenAI-compatible path, because the features that make
Claude the quality option here have no OpenAI equivalent: `output_config.format` structured
outputs, adaptive thinking with an effort dial, prompt caching, and the Batches API.
"""

from __future__ import annotations

import json

from ..config import Settings
from .base import Provider, ProviderError, RefusalError, TruncatedError, Usage


class ClaudeProvider(Provider):
    supports_batch_api = True

    def __init__(self, settings: Settings, effort: str = "medium", max_tokens: int = 16000):
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover
            raise ProviderError("the `anthropic` package is required — pip install anthropic") from e

        self.name = settings.provider
        self.label = settings.label
        self.model = settings.model
        self.effort = effort
        self.max_tokens = max_tokens
        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=settings.api_key)

    def system_blocks(self, system: str) -> list[dict]:
        # cache_control is a no-op below the model's minimum cacheable prefix (4096 tokens on
        # Opus 4.8); it only starts paying once a large glossary or production notes is used.
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def request_params(self, system: str, user: str, schema: dict) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system_blocks(system),
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            "thinking": {"type": "adaptive"},
        }

    def complete(self, system: str, user: str, schema: dict) -> tuple[dict, Usage]:
        try:
            resp = self._client.messages.create(**self.request_params(system, user, schema))
        except self._anthropic.APIStatusError as e:
            raise ProviderError(f"{e.status_code}: {e.message}") from e

        usage = Usage(requests=1)
        if resp.usage:
            usage.input_tokens = resp.usage.input_tokens or 0
            usage.output_tokens = resp.usage.output_tokens or 0
            usage.cache_read = resp.usage.cache_read_input_tokens or 0
            usage.cache_write = resp.usage.cache_creation_input_tokens or 0

        if resp.stop_reason == "refusal":
            cat = f" ({resp.stop_details.category})" if resp.stop_details else ""
            raise RefusalError(f"Claude declined to translate this batch{cat}")
        if resp.stop_reason == "max_tokens":
            raise TruncatedError("reply hit the token ceiling — lower --batch-size")

        return parse_message(resp), usage

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self._client.models.list())

    # -- Batches API: 50% cheaper, minutes-to-hours. Anthropic-only. -------

    def submit_batch(self, items: list[tuple[str, str, str]], schema: dict) -> str:
        """items = [(custom_id, system, user)] -> batch id."""
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        requests = [
            Request(
                custom_id=cid,
                params=MessageCreateParamsNonStreaming(
                    **self.request_params(system, user, schema)
                ),
            )
            for cid, system, user in items
        ]
        return self._client.messages.batches.create(requests=requests).id

    def batch_status(self, batch_id: str):
        return self._client.messages.batches.retrieve(batch_id)

    def batch_results(self, batch_id: str) -> tuple[dict[str, dict], Usage]:
        """-> ({custom_id: parsed json}, usage). Raises on any failed request."""
        out: dict[str, dict] = {}
        usage = Usage()
        for result in self._client.messages.batches.results(batch_id):
            if result.result.type != "succeeded":
                raise ProviderError(f"{result.custom_id}: {result.result.type}")
            msg = result.result.message
            out[result.custom_id] = parse_message(msg)
            if msg.usage:
                usage.requests += 1
                usage.input_tokens += msg.usage.input_tokens or 0
                usage.output_tokens += msg.usage.output_tokens or 0
        return out, usage


def parse_message(message) -> dict:
    """Pull the JSON payload out of an Anthropic message."""
    text = next((b.text for b in message.content if b.type == "text"), None)
    if not text:
        raise ProviderError("no text block in response")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ProviderError(f"response was not valid JSON: {e}") from e
