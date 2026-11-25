#!/bin/bash
# Runs the job agent. Extra arguments are passed through, e.g.  ./run.sh --limit 10
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "Not set up yet. Run ./setup.sh first."
  exit 1
fi
exec .venv/bin/python jobagent.py "$@"
