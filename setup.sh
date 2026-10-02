#!/usr/bin/env bash
# One-time setup: finds Python 3.11/3.12, creates .venv, installs dependencies.
# Usage: ./setup.sh             (core)
#        ./setup.sh --wakeword  (also the optional "Hey Jarvis" wake word)
set -euo pipefail
cd "$(dirname "$0")"

WAKEWORD=0
for arg in "$@"; do
  case "$arg" in
    --wakeword) WAKEWORD=1 ;;
    -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
  esac
done

if [[ "$(uname)" != "Darwin" ]]; then
  echo "Note: Jarvis is built for macOS. On other systems only the terminal features work."
fi

good_version() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) else 1)' >/dev/null 2>&1
}

find_python() {
  local candidates=(
    python3.12 python3.11
    /usr/local/bin/python3.12 /usr/local/bin/python3.11
    /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11
    /usr/local/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.11/bin/python3.11
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
    python3
  )
  local c path
  for c in "${candidates[@]}"; do
    path="$(command -v "$c" 2>/dev/null || true)"
    if [[ -n "$path" ]] && good_version "$path"; then
      echo "$path"
      return 0
    fi
  done
  return 1
}

if ! PY="$(find_python)"; then
  echo "Jarvis needs Python 3.11 or 3.12."
  echo "(Python 3.13 doesn't work on Intel Macs yet: onnxruntime has no Intel build for it.)"
  echo
  echo "Install one of these, then run ./setup.sh again:"
  echo "  Homebrew:   brew install python@3.12"
  echo "  Installer:  https://www.python.org/downloads/macos/  (pick a 3.12.x release)"
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

if [[ -x .venv/bin/python ]] && ! good_version .venv/bin/python; then
  echo "Existing .venv uses an unsupported Python; recreating it."
  rm -rf .venv
fi
if [[ ! -x .venv/bin/python ]]; then
  "$PY" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip wheel >/dev/null
echo "Installing dependencies (a few minutes the first time)..."
.venv/bin/python -m pip install --prefer-binary -r requirements.txt
if (( WAKEWORD )); then
  if [[ -f requirements-wakeword.txt ]]; then
    echo "Installing the wake word..."
    .venv/bin/python -m pip install --prefer-binary -r requirements-wakeword.txt
  else
    echo "requirements-wakeword.txt is missing, so the wake word was skipped. Update Jarvis and try again."
  fi
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env: paste your Anthropic API key into it."
fi
if [[ ! -f config.local.yaml ]]; then
  cat > config.local.yaml <<'LOCAL'
# Your personal Jarvis settings. Anything here overrides config.yaml.
# Copy only the settings you want to change from config.yaml, keeping the same layout.
# Git ignores this file and updates never overwrite it.
#
# Example:
# user_name: "Tony"
# llm:
#   model: claude-haiku-4-5-20251001
LOCAL
fi
mkdir -p data
chmod +x run.sh setup.sh 2>/dev/null || true

cat <<'DONE'

Setup finished. Next:
  1. Put your API key in .env        (open -e .env)
  2. Check everything:               ./run.sh --doctor

Your own settings go in config.local.yaml (see config.yaml for every option).
DONE
