#!/usr/bin/env bash
# Start Jarvis. Extra arguments are passed through, e.g. ./run.sh --cli  or  ./run.sh --doctor
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Run ./setup.sh first."
  exit 1
fi
exec .venv/bin/python -m jarvis "$@"
