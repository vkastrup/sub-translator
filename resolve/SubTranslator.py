"""
SubTranslator — DaVinci Resolve Workflow Integration script.

Translates a subtitle track on the current timeline and delivers the result as a new
subtitle track on a new timeline named "<timeline> [XX]".

Install (macOS):
    cp SubTranslator.py "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins/"
then set SUBTRANS_DIR below, and run it from
    Workspace -> Workflow Integrations -> SubTranslator

Why a new timeline: Resolve's scripting API can read subtitle cues but cannot create them.
AppendToTimeline ignores trackIndex and recordFrame for subtitle media, and locking the
other tracks turns the append into a silent no-op. The only frame-accurate route is
Resolve's own .drt format — export the timeline, clone the subtitle track with translated
text, re-import. ImportTimelineFromFile always makes a new timeline, so that is what you get.
The source timeline is never modified.

This file runs under Resolve's embedded Python, which cannot see the project venv, so it
imports nothing beyond the standard library plus subtrans.resolve_drt (also stdlib-only).
The translation itself is run as a subprocess against the venv.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import traceback

# ── Set this to wherever you cloned the project ────────────────────────────────
SUBTRANS_DIR = "/Volumes/RGBa2025/DevFolder/sub_translator"
# ───────────────────────────────────────────────────────────────────────────────

VENV_PYTHON = os.path.join(SUBTRANS_DIR, "venv", "bin", "python")

if SUBTRANS_DIR not in sys.path:
    sys.path.insert(0, SUBTRANS_DIR)

from subtrans.config import ConfigError, resolve as resolve_config  # noqa: E402
from subtrans.langtags import relabel  # noqa: E402  (needs sys.path first)
from subtrans.resolve_drt import DrtTimeline  # noqa: E402

LANGUAGES = [
    ("sv", "Swedish"), ("da", "Danish"), ("no", "Norwegian"), ("fi", "Finnish"),
    ("is", "Icelandic"), ("en", "English"), ("de", "German"), ("fr", "French"),
    ("es", "Spanish"), ("it", "Italian"), ("nl", "Dutch"), ("pt", "Portuguese"),
    ("pl", "Polish"), ("cs", "Czech"), ("ja", "Japanese"), ("ko", "Korean"),
    ("zh", "Chinese (Simplified)"), ("ar", "Arabic"),
]

WIN_ID = "com.rgba.subtranslator"


# ───────────────────────────────────────────────────────── srt (stdlib only)

def _ms_to_srt(ms):
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def write_srt(cues, fps, start_frame, path):
    """Serialise DrtCue objects to SRT. Times are relative to the timeline start."""
    blocks = []
    for i, c in enumerate(cues, 1):
        start = (c.start - start_frame) / fps * 1000.0
        end = (c.end - start_frame) / fps * 1000.0
        blocks.append("%d\n%s --> %s\n%s\n" % (i, _ms_to_srt(start), _ms_to_srt(end), c.text))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(blocks))


def read_srt_texts(path):
    """Return cue texts in order. Only the text matters — timing comes from the DRT."""
    with open(path, encoding="utf-8-sig") as f:
        raw = f.read().replace("\r\n", "\n").replace("\r", "\n")
    texts = []
    for block in raw.strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) >= 3 and "-->" in lines[1]:
            texts.append("\n".join(lines[2:]).strip())
    return texts


def timeline_fps(tl):
    """Real playback rate. Resolve reports 30.0/60.0 for the NTSC rates, which are 1000/1001
    slower; SRT timings are wall-clock, so correct for it."""
    try:
        fps = float(tl.GetSetting("timelineFrameRate"))
    except (TypeError, ValueError):
        return 24.0
    if tl.GetSetting("timelineDropFrameTimecode") == "1" or round(fps) in (24, 30, 60, 120):
        # 23.976 / 29.97 / 59.94 are stored as their rounded nominal value.
        nominal = round(fps)
        if abs(fps - nominal) < 0.001 and nominal in (24, 30, 60, 120):
            if tl.GetSetting("timelineDropFrameTimecode") == "1":
                return nominal * 1000.0 / 1001.0
    return fps


def subtitle_track_labels(tl):
    """['1: Subtitle 1 [DA] (133 cues)', ...] for the source-track dropdown.

    Module level so it can be exercised without a UI.
    """
    if not tl:
        return []
    return [
        "%d: %s (%d cues)" % (i, tl.GetTrackName("subtitle", i),
                              len(tl.GetItemListInTrack("subtitle", i) or []))
        for i in range(1, tl.GetTrackCount("subtitle") + 1)
    ]


# ───────────────────────────────────────────────────────────────── the work

def run_translation(resolve_app, project, tl, track_index, target, cpl, effort, source, say):
    mp = project.GetMediaPool()
    tmp = tempfile.mkdtemp(prefix="subtrans_")
    # "Timeline 1 [DA]" -> "Timeline 1 [SV]", not "Timeline 1 [DA] [SV]".
    wanted_name = relabel(tl.GetName(), target)
    drt_in = os.path.join(tmp, "source.drt")
    # ImportTimelineFromFile names the timeline after the file, so name the file well.
    drt_out = os.path.join(tmp, wanted_name + ".drt")
    srt_in = os.path.join(tmp, "source.srt")
    srt_out = os.path.join(tmp, "translated.srt")

    # Fail before the export if the engine isn't configured — a missing key should read as
    # one actionable sentence, not a traceback from inside an SDK 90 seconds later.
    try:
        settings = resolve_config()
        say("engine: %s (%s)" % (settings.label, settings.model))
    except ConfigError as e:
        raise RuntimeError(str(e))

    say("exporting timeline...")
    if not tl.Export(drt_in, resolve_app.EXPORT_DRT):
        raise RuntimeError("timeline export failed")

    with DrtTimeline(drt_in) as drt:
        tracks = drt.subtitle_tracks
        match = [t for t in tracks if t.index == track_index]
        if not match:
            raise RuntimeError("subtitle track %d not found in the export" % track_index)
        source_track = match[0]
        if not source_track.cues:
            raise RuntimeError("subtitle track %d is empty" % track_index)

        fps = timeline_fps(tl)
        say("%d cues on '%s' (%.3f fps)" % (len(source_track), source_track.name, fps))
        write_srt(source_track.cues, fps, tl.GetStartFrame(), srt_in)

        cmd = [
            VENV_PYTHON, "-m", "subtrans.cli",
            "--input", srt_in, "--output", srt_out,
            "--target", target, "--cpl", str(cpl), "--effort", effort,
        ]
        if source:
            cmd += ["--source", source]

        say("translating to %s..." % target.upper())
        # Resolve launches with no locale set, so the default pipe encoding is ASCII and
        # any non-ASCII byte — which is every interesting subtitle — raises. Pin UTF-8 on
        # both ends: `encoding` for our side, PYTHONIOENCODING for the child's.
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            cmd, cwd=SUBTRANS_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        for line in proc.stdout:
            line = line.strip()
            if line:
                say("  " + line)
        if proc.wait() != 0:
            raise RuntimeError("translation failed — see the log above")

        texts = read_srt_texts(srt_out)
        if len(texts) != len(source_track.cues):
            raise RuntimeError(
                "got %d translated cues but the track has %d" % (len(texts), len(source_track.cues))
            )

        name = relabel(source_track.name, target)
        drt.add_translated_track(source_track.index, texts, name)
        drt.save(drt_out)

    say("importing...")
    # The "timelineName" import option is documented as not valid for DRT, so rename after.
    new_tl = mp.ImportTimelineFromFile(drt_out)
    if not new_tl:
        raise RuntimeError("ImportTimelineFromFile returned nothing")
    if new_tl.GetName() != wanted_name:
        new_tl.SetName(wanted_name)  # returns False if the name is taken; harmless

    n = new_tl.GetTrackCount("subtitle")
    say("created timeline '%s' with %d subtitle tracks" % (new_tl.GetName(), n))

    # The clone is appended last in SubtitleTrackVec, so it is the highest index. Mute the
    # pre-existing tracks so the new one is what actually plays back — otherwise Resolve
    # renders every enabled subtitle track stacked on top of each other.
    for i in range(1, n):
        if new_tl.SetTrackEnable("subtitle", i, False):
            say("disabled source track %d '%s'" % (i, new_tl.GetTrackName("subtitle", i)))
    new_tl.SetTrackEnable("subtitle", n, True)

    say("translated track: '%s' (%d cues)" % (name, len(texts)))
    shutil.rmtree(tmp, ignore_errors=True)  # left in place if anything above raised
    return new_tl


# ────────────────────────────────────────────────────────────────────── ui

def main():
    ui = fusion.UIManager
    disp = bmd.UIDispatcher(ui)

    existing = ui.FindWindow(WIN_ID)
    if existing:
        existing.Show()
        existing.Raise()
        return

    if not project.GetCurrentTimeline():
        print("SubTranslator: no timeline open")
        return

    win = disp.AddWindow(
        {"ID": WIN_ID, "WindowTitle": "SubTranslator", "Geometry": [200, 200, 620, 480]},
        ui.VGroup([
            ui.Label({"ID": "TlName", "Text": "Timeline: —", "Weight": 0}),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Source track", "Weight": 0.3}),
                ui.ComboBox({"ID": "Track", "Weight": 0.7}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Translate to", "Weight": 0.3}),
                ui.ComboBox({"ID": "Lang", "Weight": 0.7}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Label({"Text": "Max chars/line", "Weight": 0.3}),
                ui.SpinBox({"ID": "Cpl", "Value": 42, "Minimum": 20, "Maximum": 80, "Weight": 0.2}),
                ui.Label({"Text": "Effort", "Weight": 0.15}),
                ui.ComboBox({"ID": "Effort", "Weight": 0.35}),
            ]),
            ui.Button({"ID": "Go", "Text": "Translate", "Weight": 0}),
            ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 1,
                         "Font": ui.Font({"Family": "Menlo", "PixelSize": 11})}),
        ]),
    )
    items = win.GetItems()

    def sync_timeline():
        """Re-read the current timeline into the panel.

        The panel is meant to be left open, and the user can switch timelines underneath it
        at any moment. Reading the timeline once at build time and closing over it means
        Translate would silently act on whichever timeline happened to be open back then.
        """
        tl = project.GetCurrentTimeline()
        labels = subtitle_track_labels(tl)
        items["TlName"].Text = "Timeline: %s" % (tl.GetName() if tl else "— none open —")

        previous = items["Track"].CurrentIndex
        items["Track"].Clear()
        if labels:
            items["Track"].AddItems(labels)
            if 0 <= previous < len(labels):
                items["Track"].CurrentIndex = previous
        return tl, labels

    for code, label in LANGUAGES:
        items["Lang"].AddItem("%s — %s" % (code, label))
    for e in ["low", "medium", "high"]:
        items["Effort"].AddItem(e)
    items["Effort"].CurrentIndex = 1

    def say(msg):
        # Mirrored to Workspace -> Console so there is a transcript after the window closes.
        # TextEdit.Append pumps the event loop, so the panel log does update live even though
        # the handler is blocking.
        print("[SubTranslator] %s" % msg)
        items["Log"].Append(msg)

    def on_close(ev):
        disp.ExitLoop()

    def on_go(ev):
        # Authoritative re-read: never trust a timeline captured when the window opened.
        tl, track_names = sync_timeline()
        if not tl:
            say("No timeline is open.")
            return
        if not track_names:
            say("'%s' has no subtitle track." % tl.GetName())
            return
        target = LANGUAGES[items["Lang"].CurrentIndex][0]
        track_index = items["Track"].CurrentIndex + 1
        cpl = int(items["Cpl"].Value)
        effort = ["low", "medium", "high"][items["Effort"].CurrentIndex]

        items["Go"].Enabled = False
        items["Go"].Text = "Working…"
        try:
            # Deliberately synchronous: Resolve's API is not thread-safe, and calling
            # Export/ImportTimelineFromFile off the main thread can take Resolve down.
            # The panel stays responsive enough to show progress because Append pumps the
            # event loop; the Translate button is disabled to prevent a second run.
            run_translation(resolve, project, tl, track_index, target, cpl, effort, None, say)
            say("done.")
        except Exception as e:
            say("ERROR: %s" % e)
            say(traceback.format_exc())
        finally:
            items["Go"].Enabled = True
            items["Go"].Text = "Translate"
            # ImportTimelineFromFile makes the imported timeline current, so by now Resolve
            # is showing something different from what this panel was built against.
            # Re-sync or the label and track list silently describe the previous timeline.
            now, _ = sync_timeline()
            if now:
                say("panel now showing: %s" % now.GetName())

    win.On[WIN_ID].Close = on_close
    win.On.Go.Clicked = on_go

    tl, track_names = sync_timeline()
    if not track_names:
        say("This timeline has no subtitle track — nothing to translate.")
    else:
        say("Ready. The source timeline is never modified; the translated track")
        say("arrives on a new timeline named '<timeline> [XX]'.")
        say("Progress appears here as it runs; a copy goes to Workspace > Console.")
        say("You can leave this window open — it re-reads the current timeline each run.")

    win.Show()
    disp.RunLoop()
    win.Hide()


# Resolve injects `resolve`, `project`, `fusion` and `bmd` when it launches this script.
# Guarded so the module can also be imported headlessly for testing.
if all(g in globals() for g in ("resolve", "project", "fusion", "bmd")):
    main()
