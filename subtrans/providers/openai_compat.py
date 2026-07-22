"""Adapter for any OpenAI-compatible chat-completions endpoint.

One adapter covers Mistral, Groq, OpenRouter, Together, DeepSeek and a local Ollama — they
all speak `/v1/chat/completions` and differ only in `base_url`, model id and how faithfully
they honour a JSON schema.

Schema enforcement degrades in three steps:

1. `response_format={"type": "json_schema", ..., "strict": True}` — decoding is constrained,
   ids cannot drift. Mistral supports this.
2. `response_format={"type": "json_object"}` — valid JSON guaranteed, shape is not.
3. Nothing but the prompt.

Steps 2 and 3 are why `engine.py` keeps its retry-then-split recovery: a provider that
returns the wrong cue ids gets corrected, then halved, rather than corrupting the file.
"""

from __future__ import annotations

import json

from ..config import Settings
from .base import Provider, ProviderError, TruncatedError, Usage


class OpenAICompatProvider(Provider):
    supports_batch_api = False

    def __init__(self, settings: Settings, effort: str = "medium", max_tokens: int = 16000):
        try:
            from openai import OpenAI
        except ImportError as e:  # pragma: no cover
            raise ProviderError("the `openai` package is required — pip install openai") from e

        self.name = settings.provider
        self.label = settings.label
        self.model = settings.model
        self.max_tokens = max_tokens
        self._strict = settings.spec.strict_schema
        self._client = OpenAI(
            api_key=settings.api_key or "not-needed",  # local servers ignore it
            base_url=settings.base_url,
            max_retries=3,
        )

    # -- schema handling ---------------------------------------------------

    def _response_format(self, schema: dict) -> dict | None:
        if self._strict:
            return {
                "type": "json_schema",
                "json_schema": {"name": "translations", "strict": True, "schema": schema},
            }
        return {"type": "json_object"}

    def complete(self, system: str, user: str, schema: dict) -> tuple[dict, Usage]:
        import openai

        if not self._strict:
            # json_object mode guarantees parseable JSON but not the shape, so the shape has
            # to be stated in the prompt instead.
            user = (
                f"{user}\n\nReply with JSON matching exactly this schema:\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )

        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        fmt = self._response_format(schema)
        if fmt:
            kwargs["response_format"] = fmt

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            # Some endpoints advertise OpenAI compatibility but reject json_schema. Retry
            # once in the weaker mode rather than failing the whole file.
            if self._strict and "response_format" in str(e).lower():
                self._strict = False
                return self.complete(system, user, schema)
            raise ProviderError(str(e)) from e
        except openai.APIStatusError as e:
            raise ProviderError(f"{e.status_code}: {e.message}") from e

        usage = Usage(requests=1)
        if resp.usage:
            usage.input_tokens = resp.usage.prompt_tokens or 0
            usage.output_tokens = resp.usage.completion_tokens or 0
            cached = getattr(getattr(resp.usage, "prompt_tokens_details", None), "cached_tokens", 0)
            usage.cache_read = cached or 0

        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise TruncatedError("reply hit the token ceiling — lower --batch-size")

        text = (choice.message.content or "").strip()
        if not text:
            raise ProviderError("empty reply")
        return _loads(text), usage

    def list_models(self) -> list[str]:
        return sorted(m.id for m in self._client.models.list().data)


def _loads(text: str) -> dict:
    """Parse JSON, tolerating the ```json fences weaker models add despite instructions."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if text.startswith("```"):
        body = text.split("\n", 1)[-1]
        body = body.rsplit("```", 1)[0]
        try:
            return json.loads(body.strip())
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise ProviderError(f"reply was not JSON: {text[:200]}")
