#!/bin/bash
# One-time setup: checks the Mac, creates a virtual environment, installs libraries.
cd "$(dirname "$0")" || exit 1
set -e

echo "== Checking this Mac"
if [ "$(uname -m)" != "arm64" ]; then
  echo "This needs an Apple silicon Mac (M1 or later)."; exit 1
fi
macos_major=$(sw_vers -productVersion | cut -d. -f1)
if [ "$macos_major" -lt 26 ]; then
  echo "macOS 26 or later is needed for Apple's on-device model (you have $(sw_vers -productVersion))."
  exit 1
fi
echo "macOS $(sw_vers -productVersion): OK"

dev_dir=$(xcode-select -p 2>/dev/null || true)
case "$dev_dir" in
  *Xcode*.app*) echo "Xcode: OK ($dev_dir)" ;;
  *) echo "Full Xcode is needed (Apple's Python SDK builds against it)."
     echo "Install Xcode from the App Store, open it once to accept the agreement, then run:"
     echo "  sudo xcode-select -s /Applications/Xcode.app/Contents/Developer"
     echo "and run ./setup.sh again."
     exit 1 ;;
esac

PY=""
for cand in python3.12 python3.11 python3.13 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && \
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY=$(command -v "$cand"); break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer wasn't found (the one that ships with macOS is too old)."
  echo "Install it with Homebrew:  brew install python@3.12   then run ./setup.sh again."
  exit 1
fi
echo "Python: $("$PY" --version) at $PY"

echo "== Creating virtual environment"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

chmod +x run.sh schedule.sh 2>/dev/null || true
echo
echo "== Checking Apple's on-device model"
.venv/bin/python -c "import apple_fm_sdk as fm; ok, why = fm.SystemLanguageModel().is_available(); print('Apple on-device model: OK' if ok else f'Apple on-device model NOT READY: {why}')" || true
echo
echo "Setup complete. Next: edit config.yaml, then try  ./run.sh --limit 15"
