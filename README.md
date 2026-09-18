# SubTranslator

Translate subtitles with an LLM — from the command line, or from inside DaVinci Resolve.

Built for shortform work (ads, B2B) where you need a second language on a delivery today and
sending it out to a human subtitler is disproportionate. It defaults to **Mistral's free
tier**, so an assistant or producer can run it without creating an account or spending
anything.

- Reads and writes **13 subtitle formats** — SRT, WebVTT, ASS/SSA, TTML/DFXP/iTT, SAMI, SCC,
  MicroDVD, MPL2, TMP, JSON
- **Timings are never touched.** Only the text changes
- A **DaVinci Resolve plugin** that translates a subtitle track off your timeline
- Works with Mistral, Claude, Groq, OpenRouter, a local Ollama, or any OpenAI-compatible
  endpoint

## Install

macOS:

```bash
git clone <this repo> && cd sub_translator
./install.sh --resolve --provider mistral --key <your key>
```

Windows (PowerShell — the same options, same result):

```powershell
git clone <this repo>; cd sub_translator
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Resolve -Provider mistral -Key <your key>
```

Get a free Mistral key at [console.mistral.ai](https://console.mistral.ai/) — no credit card
needed. Leave off `--resolve` / `-Resolve` if you only want the command line tool.

Resolve integration requires **Resolve Studio**; Workflow Integrations aren't available in the
free version, and Blackmagic doesn't support them on Linux at all. Installing the plugin on
Windows writes into `%PROGRAMDATA%`, so run that shell as Administrator; on macOS it writes
into `/Library/Application Support`. Python 3.9+ is needed on both — on Windows, install it
from [python.org](https://www.python.org/downloads/windows/) with *Add python.exe to PATH*
ticked.

## Using it in Resolve

**Workspace → Workflow Integrations → SubTranslator.** Pick the subtitle track, pick a
language, hit Translate. A 130-cue file takes about a minute.

You get a new timeline called `<name> [SV]` containing everything from the original plus the
translated subtitle track, with the source track muted so the new one plays. Your original
timeline is never modified.

You can leave the window open while you work — it re-reads whichever timeline is current each
time you translate.

## Using it from the command line

```bash
venv/bin/python -m subtrans.cli --input in.srt --source da --target sv
venv/bin/python -m subtrans.cli --input in.srt --target de --output out.vtt
venv/bin/python -m subtrans.cli --directory ./deliverables --pattern '*_EN.srt' --target fr
```

On Windows the interpreter is `venv\Scripts\python` instead of `venv/bin/python`; every
flag below is identical.

Output format follows the extension you ask for, so `--output out.vtt` converts as it
translates. Naming is handled for you: `spot_EN.srt` becomes `spot_FR.srt`, and an existing
language tag is replaced rather than stacked.

Useful flags:

| | |
|---|---|
| `--cpl 42` | max characters per line |
| `--max-lines 2` | max lines per cue |
| `--source da` | source language — optional, but improves quality |
| `--glossary terms.json` | force specific renderings of names or product terms |
| `--list-providers` / `--list-formats` / `--list-models` | what's available |

## Try it

`examples/` holds a short invented piece — a fictional Danish furniture company — with
English and Swedish reference translations:

```bash
venv/bin/python -m subtrans.cli --input examples/demo_DA.srt --source da --target sv \
  --output /tmp/demo_SV.srt

diff /tmp/demo_SV.srt examples/demo_SV.expected.srt
```

Twelve cues, so it's a single request and costs nothing on the free tier. It deliberately
includes the things that break subtitle translation: two-line cues, dialogue dashes, italics,
a brand name that must survive untranslated, and a cue too short for a literal translation to
fit.

The `.expected.srt` files are there to compare against, not to assert on. A good translation
will differ from them in wording — what should match is cue count, timings, line lengths and
preserved markup. They're machine-authored, so treat this as a smoke test, not a measure of
professional subtitling quality.

## Providers

| name | key | notes |
|---|---|---|
| `mistral` | `MISTRAL_API_KEY` | **default** — free tier, no credit card |
| `claude` | `ANTHROPIC_API_KEY` | paid, highest quality |
| `groq` | `GROQ_API_KEY` | free, fast |
| `openrouter` | `OPENROUTER_API_KEY` | many models; set `--model` |
| `ollama` | — | runs locally, no key |
| `custom` | `SUBTRANS_API_KEY` | any OpenAI-compatible endpoint |

Switch with `--provider claude`, or set `SUBTRANS_PROVIDER` in `.env` to change the default
for everyone on that machine.

On a 133-cue Danish documentary translated to Swedish, Mistral's free tier was competitive
with Claude and occasionally closer to the human reference. Both models kept every line under
42 characters; the professional subtitler had seven over.

## Translation quality

Cues are translated in batches, each one seeing the previous few already-translated lines.
That context is what keeps pronouns, formality and character voice consistent across a file —
translating line by line can't do it, because it never sees what came before.

The subtitle rules are enforced as part of the job: line length and line count limits,
condensing rather than overflowing when a cue is short, keeping `-` dialogue dashes and
italics, and leaving proper nouns alone. Cue count and timing are guaranteed to survive
intact — the tool will retry and subdivide a batch rather than hand back a file that has
drifted out of sync.

## Notes

**Why a new timeline?** Resolve's scripting API can read subtitle cues but has no way to
create them, so the plugin round-trips through Resolve's own timeline format and re-imports —
which always produces a new timeline. It's a limitation of Resolve, not a design choice.
