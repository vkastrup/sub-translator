"""Batched subtitle translation, independent of which model runs it.

Cues are translated in batches with a rolling window of already-translated neighbours as
context — that is what keeps pronouns, register and character voice consistent across a
file, which per-cue translation cannot do.

Each translation is pinned to its source cue id. A batch whose ids don't round-trip is
re-requested with a correction, then split in half, before it is allowed to fail. That
recovery is the reason a cheap or weak model is safe to use here: the worst case is more
requests, not a desynchronised subtitle file. `Usage.retries` / `Usage.splits` count how
often it fires — on an unfamiliar model, watch that number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .config import Settings, resolve
from .model import Cue, Subtitle
from .prompts import RESPONSE_SCHEMA, system_prompt, user_message
from .providers import Provider, ProviderError, RefusalError, TruncatedError, Usage, build

Progress = Callable[[str], None]


class TranslationError(RuntimeError):
    pass


@dataclass
class Translator:
    target: str
    source: str | None = None
    provider: Provider | None = None
    settings: Settings | None = None
    batch_size: int = 40
    context_size: int = 5
    max_chars_per_line: int = 42
    max_lines: int = 2
    effort: str = "medium"
    glossary: dict[str, str] | None = None
    notes: str | None = None
    max_retries: int = 2
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        if self.provider is None:
            self.settings = self.settings or resolve()
            self.provider = build(self.settings, effort=self.effort)
        self._system = system_prompt(
            self.target,
            self.source,
            self.max_chars_per_line,
            self.max_lines,
            self.glossary,
            self.notes,
        )

    # ------------------------------------------------------------------ public

    def translate(self, sub: Subtitle, progress: Progress | None = None) -> Subtitle:
        """Return a copy of `sub` with translated text and untouched timings."""
        say = progress or (lambda _m: None)
        say(f"engine: {self.provider}")

        cues = sub.cues
        done: dict[int, str] = {}
        history: list[tuple[Cue, str]] = []

        for start in range(0, len(cues), self.batch_size):
            batch = cues[start : start + self.batch_size]
            say(f"translating cues {batch[0].index}-{batch[-1].index} of {len(cues)}")
            out = self._translate_batch(batch, history[-self.context_size :], say)
            done.update(out)
            history.extend((c, out[c.index]) for c in batch if c.index in out)

        missing = [c.index for c in cues if c.index not in done]
        if missing:
            raise TranslationError(f"{len(missing)} cue(s) came back untranslated: {missing[:10]}")

        say(f"done — {self.usage.summary()}")
        return Subtitle(
            cues=[c.with_text(done[c.index]) for c in cues],
            language=self.target,
            source_format=sub.source_format,
        )

    # ----------------------------------------------------------------- internal

    def _translate_batch(
        self, batch: list[Cue], context: list[tuple[Cue, str]], say: Progress
    ) -> dict[int, str]:
        want = {c.index for c in batch}
        correction = ""

        for attempt in range(self.max_retries + 1):
            try:
                got = self._request(batch, context, correction)
            except RefusalError:
                raise  # retrying identical content will not help
            except (ProviderError, TruncatedError) as e:
                if attempt == self.max_retries:
                    raise TranslationError(f"provider failed after {attempt + 1} tries: {e}") from e
                self.usage.retries += 1
                correction = f"The previous request failed with: {e}"
                continue

            if set(got) == want:
                return got

            missing = sorted(want - set(got))
            extra = sorted(set(got) - want)
            correction = (
                "Your previous reply did not return exactly one translation per requested id. "
                f"Missing ids: {missing[:20]}. Unexpected ids: {extra[:20]}. "
                "Return every requested id exactly once."
            )
            if attempt < self.max_retries:
                self.usage.retries += 1
                say(f"  id mismatch ({len(missing)} missing) — retrying")
                continue

            # Last resort: halve the batch rather than fail the file.
            if len(batch) > 1:
                self.usage.splits += 1
                mid = len(batch) // 2
                say(f"  splitting batch of {len(batch)} after repeated id mismatch")
                out = self._translate_batch(batch[:mid], context, say)
                out.update(self._translate_batch(batch[mid:], context, say))
                return out
            raise TranslationError(f"cue {batch[0].index} could not be translated: {correction}")
        raise TranslationError("unreachable")

    def _request(
        self, batch: list[Cue], context: list[tuple[Cue, str]], correction: str
    ) -> dict[int, str]:
        content = user_message(batch, context)
        if correction:
            content = f"{correction}\n\n{content}"
        data, usage = self.provider.complete(self._system, content, RESPONSE_SCHEMA)
        self.usage.add(usage)
        return extract_translations(data)


    # ------------------------------------------------------------------- batch

    def translate_via_batch_api(self, sub: Subtitle, poll: Progress | None = None,
                                interval: int = 30) -> Subtitle:
        """Half-price bulk path. Anthropic only, and slower by orders of magnitude.

        Each request is independent here, so cross-batch context is lost — this trades some
        consistency for cost. Use it for back-catalogue work, not interactively.
        """
        import time

        say = poll or (lambda _m: None)
        if not self.provider.supports_batch_api:
            raise TranslationError(
                f"{self.provider.label} has no batch API — drop --batch, or use --provider claude"
            )

        items = []
        for start in range(0, len(sub.cues), self.batch_size):
            batch = sub.cues[start : start + self.batch_size]
            ctx = sub.cues[max(0, start - self.context_size) : start]
            items.append((
                f"cues-{batch[0].index}-{batch[-1].index}",
                self._system,
                user_message(batch, [(c, c.text) for c in ctx]),
            ))

        batch_id = self.provider.submit_batch(items, RESPONSE_SCHEMA)
        say(f"submitted batch {batch_id} ({len(items)} requests)")
        while True:
            status = self.provider.batch_status(batch_id)
            if status.processing_status == "ended":
                break
            say(f"  {status.processing_status}: {status.request_counts.processing} in flight")
            time.sleep(interval)

        results, usage = self.provider.batch_results(batch_id)
        self.usage.add(usage)

        done: dict[int, str] = {}
        for payload in results.values():
            done.update(extract_translations(payload))
        missing = [c.index for c in sub.cues if c.index not in done]
        if missing:
            raise TranslationError(f"{len(missing)} cue(s) missing from batch results: {missing[:10]}")

        say(f"done — {self.usage.summary()}")
        return Subtitle(
            cues=[c.with_text(done[c.index]) for c in sub.cues],
            language=self.target,
            source_format=sub.source_format,
        )


def extract_translations(data: dict) -> dict[int, str]:
    """{"translations": [{"id": 1, "text": "..."}]} -> {1: "..."}"""
    out: dict[int, str] = {}
    for item in data.get("translations") or []:
        try:
            out[int(item["id"])] = str(item["text"])
        except (KeyError, TypeError, ValueError):
            continue
    return out
