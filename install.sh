#!/bin/sh
# Sets up nanit-bridge and walks you through connecting Nanit to Miara.
# Run it again any time: it keeps what's already done and asks before changing it.
set -e
cd "$(dirname "$0")"

works() { [ -x .venv/bin/python ] && .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; }

if ! works; then
  echo "Getting Python ready (once, about a minute)…"
  rm -rf .venv
  for p in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      "$p" -m venv .venv >/dev/null 2>&1 && works && break
      rm -rf .venv
    fi
  done
fi

if ! works; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "This needs Python 3.10 or newer, and this computer doesn't have a working one."
    printf "Install uv (Astral's Python installer) for your user now? [Y/n] "
    read -r answer
    case "$answer" in [nN]*) echo "OK. Install Python 3.10 or newer, then run ./install.sh again."; exit 1;; esac
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    PATH="$HOME/.local/bin:$PATH"
  fi
  uv venv -q -p 3.13 --managed-python .venv
fi

if ! .venv/bin/python -c 'import aionanit, requests' 2>/dev/null; then
  echo "Installing what the bridge needs…"
  if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m pip install -q -r requirements.txt
  else
    uv pip install -q -p .venv -r requirements.txt
  fi
fi

exec .venv/bin/python bridge.py setup
