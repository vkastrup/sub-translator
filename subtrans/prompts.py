"""Prompt construction for subtitle translation.

The system prompt is deliberately assembled once per job and kept byte-stable across every
request in that job, so it can act as a prompt-cache prefix.
"""

from __future__ import annotations

import json

from .model import Cue

LANGUAGES = {
    "ar": "Arabic", "bg": "Bulgarian", "cs": "Czech", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "en-gb": "British English", "en-us": "American English",
    "es": "Spanish", "es-419": "Latin American Spanish", "et": "Estonian", "fi": "Finnish",
    "fr": "French", "fr-ca": "Canadian French", "he": "Hebrew", "hi": "Hindi",
    "hr": "Croatian", "hu": "Hungarian", "id": "Indonesian", "is": "Icelandic",
    "it": "Italian", "ja": "Japanese", "ko": "Korean", "lt": "Lithuanian", "lv": "Latvian",
    "nl": "Dutch", "no": "Norwegian", "nb": "Norwegian Bokmål", "pl": "Polish",
    "pt": "Portuguese", "pt-br": "Brazilian Portuguese", "ro": "Romanian", "ru": "Russian",
    "sk": "Slovak", "sl": "Slovenian", "sr": "Serbian", "sv": "Swedish", "th": "Thai",
    "tr": "Turkish", "uk": "Ukrainian", "vi": "Vietnamese", "zh": "Chinese (Simplified)",
    "zh-hant": "Chinese (Traditional)",
}


def language_name(code: str) -> str:
    return LANGUAGES.get(code.lower().strip(), code)


# The response shape. Structured outputs guarantee the ids round-trip, which is what stops
# a batch from silently losing or merging cues.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


def system_prompt(
    target: str,
    source: str | None = None,
    max_chars_per_line: int = 42,
    max_lines: int = 2,
    glossary: dict[str, str] | None = None,
    notes: str | None = None,
) -> str:
    src = f"from {language_name(source)} " if source else ""
    parts = [
        f"You are a professional subtitle translator working {src}into "
        f"{language_name(target)} for broadcast and streaming delivery.",
        "",
        "You will receive JSON containing subtitle cues. Return a translation for every cue "
        "in `translate`, keyed by the same id. Cues in `context` are neighbouring lines "
        "provided only so you can keep continuity — never return translations for them.",
        "",
        "Rules:",
        "1. Return exactly one translation per requested id. Never merge, split, drop, or "
        "reorder cues — the timing belongs to the id, and changing the count desyncs the file.",
        "2. Translate meaning, not words. Subtitles are read under time pressure: prefer the "
        "natural, idiomatic phrasing a native speaker would say over a literal rendering.",
        f"3. Keep each line at or under {max_chars_per_line} characters, and use at most "
        f"{max_lines} lines per cue. Use a newline (\\n) for the line break, and break at a "
        "clause boundary rather than mid-phrase.",
        "4. A cue that is shorter on screen needs a shorter translation. Condense rather than "
        "overflow — drop filler and redundancy before you drop meaning.",
        "5. Preserve a leading '-' on each line: it marks a change of speaker.",
        # The reader normalises SRT's <i> to ASS override tags, so that is the form the model
        # actually receives — naming only <i> here left italics unprotected in practice.
        "6. Preserve inline markup exactly, wrapped around the equivalent words in your "
        "translation. It may appear as <i>...</i> or as ASS override tags like "
        "{\\i1}...{\\i0}; return whichever form you were given, unchanged.",
        "7. Keep proper nouns, brand names, and on-screen text as they are unless the target "
        "language has an established form.",
        "8. Carry register, formality, and each character's voice consistently across cues. "
        "Once you choose a form of address (formal/informal), keep it for that pair of speakers.",
        "9. If a cue is only punctuation, a sound effect, or already in the target language, "
        "return it unchanged.",
        "10. Return only the translation text — no notes, quotes, or explanations.",
    ]
    if glossary:
        parts += ["", "Glossary — always use these renderings:"]
        parts += [f"  {k} -> {v}" for k, v in sorted(glossary.items())]
    if notes:
        parts += ["", "Production notes:", notes]
    return "\n".join(parts)


def user_message(batch: list[Cue], context: list[tuple[Cue, str]]) -> str:
    """One request payload: the cues to translate plus already-translated neighbours."""
    payload = {
        "context": [
            {"id": cue.index, "source": cue.text, "translation": translated}
            for cue, translated in context
        ],
        "translate": [
            {"id": cue.index, "text": cue.text, "duration_ms": cue.duration_ms}
            for cue in batch
        ],
    }
    if not payload["context"]:
        payload.pop("context")
    return json.dumps(payload, ensure_ascii=False, indent=1)
