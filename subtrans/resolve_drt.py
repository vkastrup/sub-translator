"""Read and rewrite the subtitle tracks inside a DaVinci Resolve .drt timeline.

**Standard library only.** This module is imported by the Resolve plugin, which runs under
Resolve's embedded Python and cannot see the project venv.

Why this exists: Resolve's scripting API can read subtitle cues but has no way to create
them. `AppendToTimeline` ignores both `trackIndex` and `recordFrame` for subtitle media, so
an imported .srt always lands on the track that already holds subtitles, appended past the
end of the timeline. The only frame-accurate route is to go through Resolve's own timeline
format: export .drt, clone the subtitle track with translated text, re-import.

A .drt is a zip. `SeqContainer/<uuid>.xml` holds:

    <SubtitleTrackVec>
      <Element>
        <Sm2TiTrack DbId="...">
          <UserDefinedName>Subtitle 1</UserDefinedName>
          <Items>
            <Element>
              <Sm2TiGenerator DbId="...">
                <PrettyType>Subtitle</PrettyType>
                <Name>first line&lt;br&gt;second line</Name>
                <Start>86401</Start>
                <Duration>69</Duration>
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field

__all__ = ["DrtTimeline", "DrtCue", "DrtTrack", "DrtError"]

# In a .drt, an in-cue line break is the literal string "<br>" inside the <Name> text.
# Resolve converts it to U+2028 when the timeline is loaded.
DRT_BREAK = "<br>"

# Resolve emits C++-style tag names such as <ListMgt::LmVersionTable>. "::" is illegal in
# an XML name, so swap it for a safe token across the parse/serialise cycle.
_ESC = re.compile(r"(</?)([A-Za-z_][\w.\-]*)::")
_UNESC = re.compile(r"(</?)([A-Za-z_][\w.\-]*)__CC__")


class DrtError(Exception):
    pass


@dataclass
class DrtCue:
    text: str  # newlines, not <br>
    start: int  # absolute timeline frame
    duration: int  # frames

    @property
    def end(self) -> int:
        return self.start + self.duration


@dataclass
class DrtTrack:
    index: int  # 1-based, as the Resolve API numbers them
    name: str
    cues: list[DrtCue] = field(default_factory=list)
    _element: ET.Element | None = None

    def __len__(self) -> int:
        return len(self.cues)


class DrtTimeline:
    """Open a .drt, inspect its subtitle tracks, add a translated one, save it back."""

    def __init__(self, path: str):
        self.path = path
        self._tmp = tempfile.mkdtemp(prefix="subtrans_drt_")
        try:
            with zipfile.ZipFile(path) as z:
                self._names = z.namelist()
                z.extractall(self._tmp)
        except zipfile.BadZipFile as e:
            raise DrtError(f"{path} is not a readable .drt archive: {e}") from e

        seqs = [n for n in self._names if n.startswith("SeqContainer/") and n.endswith(".xml")]
        if not seqs:
            raise DrtError(f"{path} contains no SeqContainer XML")
        self._seq_path = os.path.join(self._tmp, seqs[0])
        self._root = self._load(self._seq_path)

        self._vec = self._root.find(".//SubtitleTrackVec")
        if self._vec is None:
            raise DrtError("timeline has no SubtitleTrackVec — it has never had a subtitle track")

    # ------------------------------------------------------------------ xml io

    @staticmethod
    def _load(path: str) -> ET.Element:
        with open(path, encoding="utf-8") as f:
            return ET.fromstring(_ESC.sub(r"\1\2__CC__", f.read()))

    @staticmethod
    def _dump(root: ET.Element, path: str) -> None:
        xml = ET.tostring(root, encoding="unicode")
        with open(path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n' + _UNESC.sub(r"\1\2::", xml))

    # --------------------------------------------------------------- inspection

    @property
    def subtitle_tracks(self) -> list[DrtTrack]:
        tracks = []
        for i, el in enumerate(self._vec.findall("Element"), 1):
            name_el = el.find(".//UserDefinedName")
            name = (name_el.text or "").strip() if name_el is not None else ""
            cues = [
                DrtCue(
                    text=(g.findtext("Name") or "").replace(DRT_BREAK, "\n"),
                    start=int(g.findtext("Start") or 0),
                    duration=int(g.findtext("Duration") or 0),
                )
                for g in el.iter("Sm2TiGenerator")
            ]
            tracks.append(DrtTrack(index=i, name=name or f"Subtitle {i}", cues=cues, _element=el))
        return tracks

    # ---------------------------------------------------------------- mutation

    def add_translated_track(self, source_index: int, texts: list[str], name: str) -> None:
        """Clone subtitle track `source_index`, replacing each cue's text, keeping timing.

        `texts` must line up one-to-one with that track's cues, in order.
        """
        tracks = self.subtitle_tracks
        match = [t for t in tracks if t.index == source_index]
        if not match:
            raise DrtError(f"no subtitle track {source_index} (found {len(tracks)})")
        source = match[0]
        if len(texts) != len(source.cues):
            raise DrtError(
                f"track {source_index} has {len(source.cues)} cues but got {len(texts)} translations"
            )

        clone = copy.deepcopy(source._element)
        # Every DbId in a project must be unique or Resolve rejects or merges the import.
        for e in clone.iter():
            if "DbId" in e.attrib:
                e.set("DbId", str(uuid.uuid4()))

        for gen, text in zip(clone.iter("Sm2TiGenerator"), texts):
            n = gen.find("Name")
            if n is not None:
                n.text = text.replace("\r\n", "\n").replace("\n", DRT_BREAK)

        udn = clone.find(".//UserDefinedName")
        if udn is None:
            track_el = clone.find("Sm2TiTrack")
            udn = ET.SubElement(track_el if track_el is not None else clone, "UserDefinedName")
        udn.text = name

        self._vec.append(clone)

    def save(self, out_path: str) -> str:
        self._dump(self._root, self._seq_path)
        if os.path.exists(out_path):
            os.remove(out_path)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            for n in self._names:
                p = os.path.join(self._tmp, n)
                if os.path.isfile(p):
                    z.write(p, n)
        return out_path

    def close(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def __enter__(self) -> "DrtTimeline":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
