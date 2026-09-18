"""Command line entry point.

    python -m subtrans.cli --input in.srt --target sv
    python -m subtrans.cli --input in.srt --target sv --output out.vtt
    python -m subtrans.cli --directory samples/ --target sv --pattern '*_DA.srt'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import formats
from .config import PROVIDERS, ConfigError, load_dotenv, resolve
from .engine import Translator
from .langtags import relabel
from .prompts import LANGUAGES, language_name
from .providers import build


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def force_utf8_io() -> None:
    """Make stdout/stderr UTF-8 regardless of platform.

    On Windows a redirected stream still defaults to the ANSI code page, so one accented
    character in a filename or a progress line ends the run with a UnicodeEncodeError —
    and the Resolve plugin always reads this process through a pipe. No-op on macOS.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # not a real stream, or already detached
            pass


def load_env() -> None:
    """Pick up provider credentials from the project .env (config.load_dotenv is stdlib)."""
    load_dotenv()


def default_output(inp: str, target: str, out_format: str | None) -> str:
    """foo_DA.srt -> foo_SV.srt; foo.srt -> foo_SV.srt."""
    p = Path(inp)
    ext = formats.extension_for(out_format) if out_format else p.suffix
    stem = relabel(p.stem, target, append="_{}")
    return str(p.parent / f"{stem}{ext}")


def build_translator(args) -> Translator:
    glossary = None
    if args.glossary:
        glossary = json.loads(Path(args.glossary).read_text(encoding="utf-8"))
    notes = Path(args.notes).read_text(encoding="utf-8") if args.notes else None
    settings = resolve(provider=args.provider, model=args.model)
    return Translator(
        target=args.target,
        source=args.source,
        settings=settings,
        batch_size=args.batch_size,
        context_size=args.context,
        max_chars_per_line=args.cpl,
        max_lines=args.max_lines,
        effort=args.effort,
        glossary=glossary,
        notes=notes,
    )


def translate_file(args, inp: str, tr: Translator) -> str:
    out = args.output or default_output(inp, args.target, args.output_format)
    sub = formats.read(inp, args.input_format, fps=args.fps)
    log(f"{Path(inp).name}: {len(sub)} cues -> {language_name(args.target)}")

    if args.batch:
        translated = tr.translate_via_batch_api(sub, poll=log)
    else:
        translated = tr.translate(sub, progress=log)

    formats.write(translated, out, args.output_format, fps=args.fps)
    log(f"wrote {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    force_utf8_io()
    ap = argparse.ArgumentParser(
        prog="subtrans", description="Translate subtitle files with Claude."
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--input", "-i", help="input subtitle file")
    src.add_argument("--directory", "-d", help="translate every matching file in a directory")
    ap.add_argument("--pattern", default="*.srt", help="glob used with --directory (default *.srt)")
    ap.add_argument("--target", "-t", help="target language code, e.g. sv")
    ap.add_argument("--source", "-s", help="source language code (optional, helps quality)")
    ap.add_argument("--output", "-o", help="output path (single file only)")
    ap.add_argument("--input-format", help=f"override input format: {', '.join(formats.supported())}")
    ap.add_argument("--output-format", help=f"output format: {', '.join(formats.supported(True))}")
    ap.add_argument("--fps", type=float, help="frame rate, required for frame-based formats")
    ap.add_argument("--cpl", type=int, default=42, help="max characters per line (default 42)")
    ap.add_argument("--max-lines", type=int, default=2, help="max lines per cue (default 2)")
    ap.add_argument("--batch-size", type=int, default=40, help="cues per request (default 40)")
    ap.add_argument("--context", type=int, default=5, help="context cues carried (default 5)")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--provider", help=f"engine: {', '.join(sorted(PROVIDERS))} (default from .env, else mistral)")
    ap.add_argument("--model", help="model id (default: the provider's)")
    ap.add_argument("--glossary", help="JSON file of {source term: target term}")
    ap.add_argument("--notes", help="text file of production notes for the prompt")
    ap.add_argument("--batch", action="store_true", help="use the Batches API (50%% cheaper, slow)")
    ap.add_argument("--list-languages", action="store_true")
    ap.add_argument("--list-formats", action="store_true")
    ap.add_argument("--list-providers", action="store_true")
    ap.add_argument("--list-models", action="store_true",
                    help="ask the configured provider what it serves")
    args = ap.parse_args(argv)

    if args.list_languages:
        for code, name in sorted(LANGUAGES.items()):
            print(f"  {code:8} {name}")
        return 0
    if args.list_formats:
        writable = set(formats.supported(True))
        for name in formats.supported():
            print(f"  {name:10} {'read/write' if name in writable else 'read only'}")
        return 0

    load_env()

    if args.list_providers:
        from .config import configured_providers

        ready = {s.name for s in configured_providers()}
        for spec in PROVIDERS.values():
            mark = "ready" if spec.name in ready else f"needs {spec.env_key}"
            free = " [free]" if spec.free else ""
            print(f"  {spec.name:11} {mark:24} {spec.default_model:24}{free}")
            if spec.notes:
                print(f"              {spec.notes}")
        return 0

    if args.list_models:
        try:
            settings = resolve(provider=args.provider, model=args.model)
            for m in build(settings).list_models():
                print(f"  {m}")
        except Exception as e:
            log(f"error: {e}")
            return 1
        return 0

    if not args.input and not args.directory:
        ap.error("one of --input or --directory is required")
    if not args.target:
        ap.error("--target is required")
    if args.output and args.directory:
        ap.error("--output cannot be used with --directory")

    try:
        tr = build_translator(args)
    except Exception as e:
        log(f"error: {e}")
        return 1

    try:
        if args.input:
            print(translate_file(args, args.input, tr))
        else:
            files = sorted(Path(args.directory).glob(args.pattern))
            if not files:
                log(f"no files matching {args.pattern!r} in {args.directory}")
                return 1
            log(f"{len(files)} file(s) to translate")
            for f in files:
                print(translate_file(args, str(f), tr))
        log(f"usage: {tr.usage.summary()}")
    except Exception as e:
        log(f"error: {type(e).__name__}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
