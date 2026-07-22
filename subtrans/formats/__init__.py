"""Read and write subtitle files as `Subtitle` objects.

Two backends, chosen per format *and per direction* — neither library is best at
everything:

* **pysubs2** — SRT, WebVTT, ASS/SSA, MicroDVD, MPL2, TMP, JSON. Pure Python and tolerant
  of the malformed files that turn up in real deliveries.
* **pycaption** — SCC (CEA-608 broadcast), DFXP/TTML/iTT, SAMI. Also used for TTML because
  pysubs2's TTML writer flattens in-cue line breaks.

Adding a format means adding one entry to `_FORMATS`, not writing a parser.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from typing import Callable

from ..model import Cue, Subtitle

__all__ = ["read", "write", "supported", "extension_for", "UnknownFormat", "LossyFormatWarning"]


class UnknownFormat(Exception):
    """Raised when a file's format cannot be determined or is not supported."""


class LossyFormatWarning(UserWarning):
    """The target format cannot represent everything in the subtitle."""


# (backend, handle) — handle is a pysubs2 format id or a pycaption class-name stem.
Backend = tuple[str, str]


@dataclass(frozen=True)
class _Format:
    name: str
    extensions: tuple[str, ...]
    reader: Backend
    writer: Backend | None = None  # None => read-only
    needs_fps: bool = False
    lossy: str | None = None


_P2 = "pysubs2"
_PC = "pycaption"

_FORMATS: tuple[_Format, ...] = (
    _Format("srt", (".srt",), (_P2, "srt"), (_P2, "srt")),
    _Format("vtt", (".vtt", ".webvtt"), (_P2, "vtt"), (_P2, "vtt")),
    _Format("ass", (".ass",), (_P2, "ass"), (_P2, "ass")),
    _Format("ssa", (".ssa",), (_P2, "ssa"), (_P2, "ssa")),
    _Format("json", (".json",), (_P2, "json"), (_P2, "json")),
    _Format("mpl2", (".mpl",), (_P2, "mpl2"), (_P2, "mpl2"),
            lossy="MPL2 stores decisecond timings; expect up to 100ms of rounding."),
    _Format("tmp", (".tmp",), (_P2, "tmp"), (_P2, "tmp"),
            lossy="TMP stores no end times; they are reconstructed on read."),
    _Format("microdvd", (".sub",), (_P2, "microdvd"), (_P2, "microdvd"), needs_fps=True),
    # pycaption handles these better than pysubs2 does
    _Format("ttml", (".ttml", ".xml"), (_PC, "DFXP"), (_PC, "DFXP")),
    _Format("dfxp", (".dfxp",), (_PC, "DFXP"), (_PC, "DFXP")),
    _Format("itt", (".itt",), (_PC, "DFXP"), (_PC, "DFXP")),  # iTT is a TTML profile
    _Format("sami", (".smi", ".sami"), (_PC, "SAMI"), (_PC, "SAMI")),
    _Format("scc", (".scc",), (_PC, "SCC"), (_PC, "SCC"),
            lossy="SCC is CEA-608: no accented characters, and timings quantise to 29.97fps."),
)

_BY_NAME = {f.name: f for f in _FORMATS}
_BY_EXT: dict[str, _Format] = {}
for _f in _FORMATS:
    for _e in _f.extensions:
        _BY_EXT.setdefault(_e, _f)


def supported(writable: bool = False) -> list[str]:
    return sorted(f.name for f in _FORMATS if f.writer or not writable)


def extension_for(fmt: str) -> str:
    try:
        return _BY_NAME[fmt].extensions[0]
    except KeyError:
        raise UnknownFormat(f"unknown format {fmt!r}; known: {', '.join(supported())}") from None


def _resolve(path: str, fmt: str | None) -> _Format:
    if fmt:
        if fmt not in _BY_NAME:
            raise UnknownFormat(f"unknown format {fmt!r}; known: {', '.join(supported())}")
        return _BY_NAME[fmt]
    ext = os.path.splitext(path)[1].lower()
    if ext not in _BY_EXT:
        raise UnknownFormat(
            f"cannot infer subtitle format from extension {ext!r}; "
            f"pass one of: {', '.join(supported())}"
        )
    return _BY_EXT[ext]


# --------------------------------------------------------------------------- pysubs2


def _pysubs2_read(path: str, handle: str, fps: float | None) -> list[Cue]:
    import pysubs2

    ssa = pysubs2.load(path, encoding="utf-8-sig", format_=handle, fps=fps)
    cues = []
    for ev in ssa:
        if ev.is_comment or ev.is_drawing:
            continue
        # pysubs2 writes MicroDVD's fps header as {0}{0}<fps> but its reader only skips
        # {1}{1}, so the declaration comes back as a zero-length cue. Drop it.
        if handle == "microdvd" and not cues and ev.start == ev.end == 0:
            try:
                float(ev.text.strip())
                continue
            except ValueError:
                pass
        text = ev.plaintext if handle in ("ass", "ssa") else ev.text
        text = text.replace("\\N", "\n").replace("\\n", "\n")
        cues.append(
            Cue(
                index=0,
                start_ms=int(ev.start),
                end_ms=int(ev.end),
                lines=[ln.rstrip() for ln in text.split("\n")],
                style=ev.style or None,
                speaker=ev.name or None,
            )
        )
    return cues


def _pysubs2_write(sub: Subtitle, path: str, handle: str, fps: float | None) -> None:
    import pysubs2

    ssa = pysubs2.SSAFile()
    for cue in sub:
        ev = pysubs2.SSAEvent(start=cue.start_ms, end=cue.end_ms)
        ev.text = "\\N".join(cue.lines) if handle in ("ass", "ssa") else cue.text
        if cue.style:
            ev.style = cue.style
        if cue.speaker:
            ev.name = cue.speaker
        ssa.append(ev)
    ssa.save(path, encoding="utf-8", format_=handle, fps=fps)


# -------------------------------------------------------------------------- pycaption

_PYCAPTION_LANG = "en-US"  # pycaption keys caption sets by language; ours is single-track


def _silence_bs4():
    """pycaption parses XML with the HTML parser and bs4 warns about it on every call."""
    try:
        from bs4 import XMLParsedAsHTMLWarning

        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    except Exception:
        pass


def _pycaption_read(path: str, handle: str, fps: float | None) -> list[Cue]:
    import pycaption

    _silence_bs4()
    raw = open(path, encoding="utf-8-sig").read()
    caps = getattr(pycaption, f"{handle}Reader")().read(raw)
    langs = caps.get_languages()
    if not langs:
        return []
    cues = []
    for c in caps.get_captions(langs[0]):
        cues.append(
            Cue(
                index=0,
                start_ms=int(c.start / 1000),  # pycaption works in microseconds
                end_ms=int(c.end / 1000),
                lines=[ln.rstrip() for ln in c.get_text().split("\n")],
            )
        )
    return cues


def _pycaption_write(sub: Subtitle, path: str, handle: str, fps: float | None) -> None:
    import pycaption
    from pycaption import Caption, CaptionList, CaptionNode, CaptionSet

    _silence_bs4()
    caps = CaptionList()
    for cue in sub:
        nodes: list[CaptionNode] = []
        for i, line in enumerate(cue.lines):
            if i:
                nodes.append(CaptionNode.create_break())
            nodes.append(CaptionNode.create_text(line))
        caps.append(Caption(cue.start_ms * 1000, cue.end_ms * 1000, nodes))
    out = getattr(pycaption, f"{handle}Writer")().write(CaptionSet({_PYCAPTION_LANG: caps}))
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)


# ----------------------------------------------------------------------------- public

_READERS: dict[str, Callable[[str, str, float | None], list[Cue]]] = {
    _P2: _pysubs2_read,
    _PC: _pycaption_read,
}
_WRITERS: dict[str, Callable[[Subtitle, str, str, float | None], None]] = {
    _P2: _pysubs2_write,
    _PC: _pycaption_write,
}


def read(path: str, fmt: str | None = None, fps: float | None = None) -> Subtitle:
    """Load a subtitle file. Format is inferred from the extension unless given.

    `fps` is required for frame-based formats (MicroDVD).
    """
    spec = _resolve(path, fmt)
    if spec.needs_fps and fps is None:
        raise UnknownFormat(f"format {spec.name!r} is frame-based — pass fps=")
    backend, handle = spec.reader
    cues = _READERS[backend](path, handle, fps)
    return Subtitle(cues=cues, source_format=spec.name).sorted().renumber()


def write(sub: Subtitle, path: str, fmt: str | None = None, fps: float | None = None) -> None:
    """Save a subtitle file. Format is inferred from the extension unless given."""
    spec = _resolve(path, fmt)
    if spec.writer is None:
        raise UnknownFormat(f"format {spec.name!r} is read-only")
    if spec.needs_fps and fps is None:
        raise UnknownFormat(f"format {spec.name!r} is frame-based — pass fps=")
    if spec.lossy:
        warnings.warn(f"{spec.name}: {spec.lossy}", LossyFormatWarning, stacklevel=2)
    backend, handle = spec.writer
    _WRITERS[backend](sub, path, handle, fps)
