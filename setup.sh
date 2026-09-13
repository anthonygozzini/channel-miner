#!/usr/bin/env bash
# One-time setup. Safe to re-run: every step is skipped if it is already done.
#
#   ./setup.sh https://www.youtube.com/@SomeChannel   # the only thing you must supply
#   ./setup.sh --with-ai <url>                        # also install ollama + a 2 GB model
#   ./setup.sh                                        # asks for the channel, or keeps the current one
#   ./setup.sh --no-stt <url>                         # skip Whisper (captioned videos only)
set -uo pipefail
cd "$(dirname "$0")"

say() { printf '\n== %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

CHANNEL=""
WITH_AI=0
for arg in "$@"; do
  case "$arg" in
    --with-ai) WITH_AI=1 ;;
    --*) ;;
    *) CHANNEL="$arg" ;;
  esac
done

say "channel"
if [ -z "$CHANNEL" ] && [ ! -f config.json ] && [ -t 0 ]; then
  printf 'YouTube channel URL (e.g. https://www.youtube.com/@SomeChannel): '
  read -r CHANNEL
fi

if [ -n "$CHANNEL" ]; then
  python3 - "$CHANNEL" <<'PYEOF'
import json, os, sys, re
url = sys.argv[1].strip().rstrip("/")
if not re.match(r"^https?://", url):
    url = "https://www.youtube.com/" + url.lstrip("/")
cfg = {}
if os.path.exists("config.json"):
    try:
        cfg = json.load(open("config.json"))
    except Exception:
        cfg = {}
cfg["channel"] = url
cfg.setdefault("language", None)      # detected from the channel on first run
cfg.setdefault("data_dir", "data")
with open("config.json", "w") as fh:
    json.dump(cfg, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print(f"config.json -> {url}")
PYEOF
elif [ -f config.json ]; then
  echo "keeping the channel already in config.json: $(python3 -c 'import json;print(json.load(open("config.json")).get("channel",""))')"
else
  echo "no channel given and no config.json — re-run as: ./setup.sh <channel-url>"
  exit 1
fi

say "yt-dlp"
if have yt-dlp; then
  echo "found: $(yt-dlp --version)"
else
  python3 -m pip install --user --upgrade yt-dlp \
    || python3 -m pip install --user --upgrade --break-system-packages yt-dlp
fi

say "deno (YouTube's JS challenge)"
# YouTube serves media URLs behind a JS challenge. yt-dlp enables only deno for it, and finds it
# on PATH — without it, every download of an uncaptioned video returns HTTP 403.
if [ -x "$HOME/.local/bin/deno" ] || have deno; then
  echo "found"
else
  mkdir -p "$HOME/.local/bin"
  tmp=$(mktemp -d)
  if curl -fsSL -o "$tmp/deno.zip" \
      "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"; then
    unzip -oq "$tmp/deno.zip" -d "$HOME/.local/bin/" && chmod +x "$HOME/.local/bin/deno"
    echo "installed to ~/.local/bin/deno"
  else
    echo "could not download deno — captioned videos still work; uncaptioned ones will 403"
  fi
  rm -rf "$tmp"
fi

SKIP_STT=0
[ "${1:-}" = "--no-stt" ] && SKIP_STT=1

if [ "$SKIP_STT" = "1" ]; then
  say "whisper — skipped (--no-stt)"
else
  say "whisper virtualenv (only needed for videos with no captions)"
  VENV="$HOME/whisper-venv"
  if [ -d "$VENV" ]; then
    echo "exists: $VENV"
  else
    python3 -m venv "$VENV" && "$VENV/bin/pip" -q install --upgrade pip \
      && "$VENV/bin/pip" -q install faster-whisper \
      && echo "created $VENV"
  fi
  SITE=$(ls -d "$VENV"/lib/python3.*/site-packages 2>/dev/null | head -1)
  [ -n "$SITE" ] && echo "set \"whisper_venv_site\": \"${SITE/#$HOME/\~}\" in config.json"

fi

say "ollama (runs the AI reading steps on THIS machine - optional)"
OLLAMA_MODEL="llama3.2:3b"      # ~2 GB, enough for the reading pass

if ! have ollama && [ "$WITH_AI" = "1" ]; then
  echo "installing ollama..."
  curl -fsSL https://ollama.com/install.sh | sh || echo "install failed - see https://ollama.com"
fi

if have ollama; then
  echo "found"
  if ! curl -fsS --max-time 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "starting it in the background..."
    (ollama serve >/dev/null 2>&1 &) ; sleep 3
  fi
  if ollama list 2>/dev/null | tail -n +2 | grep -q .; then
    ollama list 2>/dev/null | tail -n +2 | awk '{print "  - " $1}'
  elif [ "$WITH_AI" = "1" ]; then
    # A model is a multi-gigabyte download, so it happens only when asked for by name.
    echo "downloading $OLLAMA_MODEL (about 2 GB, once)..."
    ollama pull "$OLLAMA_MODEL" && python3 - "$OLLAMA_MODEL" <<'PYEOF'
import json, sys
cfg = json.load(open("config.json"))
cfg["model"] = sys.argv[1]
with open("config.json", "w") as fh:
    json.dump(cfg, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
print(f"config.json -> model {sys.argv[1]}")
PYEOF
  else
    echo "no model downloaded yet - re-run with --with-ai, or:"
    echo "    ollama pull llama3.2:3b     # small and fast, usually enough"
    echo "    ollama pull qwen3.5:9b      # better, needs ~7 GB of memory"
  fi
  if curl -fsS --max-time 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "answering on localhost:11434 - ready"
  else
    echo "installed but not answering on localhost:11434 (start it: ollama serve)"
  fi
else
  echo "NOT installed - the transcripts and the index work fine without it;"
  echo "only the reading and judging steps need a model."
  echo "  re-run as: ./setup.sh --with-ai <channel-url>   (installs ollama + a 2 GB model)"
fi

say "done — now run: ./run.sh --no-read   (or ./run.sh once a model is in place)"
