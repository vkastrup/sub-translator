#!/usr/bin/env bash
# subtrans installer.
#
#   ./install.sh                                    venv + dependencies
#   ./install.sh --resolve                          also install the Resolve plugin
#   ./install.sh --resolve --provider mistral --key XXXX
#                                                   deploy to a workstation, no prompts
#
# The --provider/--key form is the one to use when setting up an edit suite: it writes the
# studio key into .env so a producer never has to create or paste an API key.
#
# Windows: use install.ps1, which takes the same options as -Resolve/-Provider/-Key.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Workflow Integration Plugins"

WITH_RESOLVE=0
PROVIDER=""
KEY=""

while [ $# -gt 0 ]; do
    case "$1" in
        --resolve)  WITH_RESOLVE=1; shift ;;
        --provider) PROVIDER="${2:-}"; shift 2 ;;
        --key)      KEY="${2:-}"; shift 2 ;;
        -h|--help)  sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

cd "$HERE"

echo "==> creating venv"
python3 -m venv venv
venv/bin/python -m pip install --quiet --upgrade pip
echo "==> installing dependencies"
venv/bin/python -m pip install --quiet -r requirements.txt
venv/bin/python -c "import anthropic, openai, pysubs2, pycaption" && echo "    ok"

# ── credentials ───────────────────────────────────────────────────────────────
[ -f .env ] || cp .env.example .env

if [ -n "$PROVIDER" ]; then
    # The .env rewrite lives in subtrans/config.py (stdlib only) so this script and
    # install.ps1 cannot drift on which env var a given provider actually reads.
    venv/bin/python -c \
        "import sys; from subtrans.config import write_env; print('==> ' + write_env(*sys.argv[1:]))" \
        "$PROVIDER" "$KEY"
    chmod 600 .env
fi

if ! venv/bin/python -m subtrans.cli --list-providers >/dev/null 2>&1; then
    echo "!! could not read the provider registry" >&2
fi

# ── Resolve plugin ────────────────────────────────────────────────────────────
if [ "$WITH_RESOLVE" = "1" ]; then
    if [ ! -d "$PLUGIN_DIR" ]; then
        echo "!! $PLUGIN_DIR does not exist — is DaVinci Resolve Studio installed?" >&2
        exit 1
    fi
    # The plugin needs to know where this checkout lives; patch the constant on install.
    # Written as a raw string literal so the Windows installer can patch the same line
    # without its backslashes being read as escapes.
    sed "s|^SUBTRANS_DIR = .*|SUBTRANS_DIR = r\"$HERE\"|" resolve/SubTranslator.py \
        > "$PLUGIN_DIR/SubTranslator.py"
    echo "==> installed plugin to Workflow Integration Plugins/"
    echo "    restart Resolve, then: Workspace > Workflow Integrations > SubTranslator"
fi

echo
echo "Configured engines:"
venv/bin/python -m subtrans.cli --list-providers 2>/dev/null | grep -E "ready" || true
cat <<EOF

  venv/bin/python -m subtrans.cli --input in.srt --target sv
  venv/bin/python -m subtrans.cli --list-providers
EOF
