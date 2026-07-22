"""Detect and replace language tags in track names, timeline names and filenames.

**Standard library only** — the Resolve plugin imports this.

The point is that `Subtitle 1 [DA]` translated to Swedish should become `Subtitle 1 [SV]`,
not `Subtitle 1 [DA] [SV]`. A name with no recognisable tag just gets one appended.
"""

from __future__ import annotations

import re

__all__ = ["relabel", "find_tag", "is_language_code", "LANGUAGE_CODES"]

# ISO 639-1, plus the common 639-2/B three-letter codes and locale forms. This set is used
# only to decide "is this trailing token a language tag?", so breadth matters more than
# precision — but keep it to real codes, or names ending in a short word get mangled.
_TWO = """
ar bg bn ca cs cy da de el en es et eu fa fi fr ga gl he hi hr hu hy id is it iw ja ka kk
km kn ko lt lv mk ml mn mr ms mt my ne nl nn no pa pl pt ro ru si sk sl sq sr sv sw ta te
th tl tr uk ur uz vi zh
""".split()

_THREE = """
ara bul ben cat ces cze dan deu ger ell gre eng spa est eus baq fas per fin fra fre gle
glg heb hin hrv hun hye arm isl ice ita jpn kat geo kaz khm kan kor lit lav mkd mac mal
mon mar msa may mlt mya bur nep nld dut nno nor pan pol por ron rum rus sin slk slo slv
sqi alb srp swe swa tam tel tha tgl tur ukr urd uzb vie zho chi
""".split()

LANGUAGE_CODES = frozenset(_TWO) | frozenset(_THREE)

# A tag is a code, optionally with a region/script suffix: pt-BR, zh-Hant, es-419, en_US.
_CODE = r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,4})?"

# "Subtitle 1 [DA]" / "(da)" / "{DA}" / "<da>"  — bracket style is preserved on replace.
_BRACKETED = re.compile(r"^(?P<head>.*?)\s*(?P<open>[\[({<])\s*(?P<code>%s)\s*(?P<close>[\])}>])\s*$" % _CODE)
# "Subtitle 1_DA" / "Subtitle 1 - DA" / "Subtitle 1.da" — separator is preserved.
_SUFFIXED = re.compile(r"^(?P<head>.*?)(?P<sep>\s*[_.\-\s]\s*)(?P<code>%s)\s*$" % _CODE)


def is_language_code(token: str) -> bool:
    """True if `token` looks like a language tag (`da`, `pt-BR`, `swe`)."""
    base = re.split(r"[-_]", token.strip(), 1)[0]
    return base.lower() in LANGUAGE_CODES


def find_tag(name: str) -> tuple[str, str] | None:
    """Return (matched_tag_text, code) for a trailing language tag, or None."""
    for pattern in (_BRACKETED, _SUFFIXED):
        m = pattern.match(name)
        if m and is_language_code(m.group("code")):
            return name[m.end("head"):], m.group("code")
    return None


def _match_case(code: str, like: str) -> str:
    """Render `code` in the case style of the tag it replaces."""
    if like.isupper():
        return code.upper()
    if like.islower():
        return code.lower()
    # Mixed, e.g. pt-BR / zh-Hant — leave the caller's preferred form alone.
    return code


def relabel(name: str, target: str, append: str = " [{}]") -> str:
    """Replace a trailing language tag in `name` with `target`, or append one.

    An existing tag keeps its bracket style, separator and letter case; `append` is only
    used when the name has no tag at all.

    >>> relabel("Subtitle 1 [DA]", "sv")
    'Subtitle 1 [SV]'
    >>> relabel("Subtitle 1 (da)", "sv")
    'Subtitle 1 (sv)'
    >>> relabel("dialogue_DA", "sv")
    'dialogue_SV'
    >>> relabel("Subtitle 1", "sv")
    'Subtitle 1 [SV]'
    >>> relabel("episode01", "sv", append="_{}")
    'episode01_SV'
    """
    name = name.rstrip()

    m = _BRACKETED.match(name)
    if m and is_language_code(m.group("code")):
        code = _match_case(target, m.group("code"))
        return f"{m.group('head')} {m.group('open')}{code}{m.group('close')}".strip()

    m = _SUFFIXED.match(name)
    if m and is_language_code(m.group("code")):
        code = _match_case(target, m.group("code"))
        return f"{m.group('head')}{m.group('sep')}{code}"

    return name + append.format(target.upper())
