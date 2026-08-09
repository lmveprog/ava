#!/bin/zsh
# double-click this from finder to start ava.
# works wherever the checkout lives — nothing hardcoded.
cd "$(dirname "$0")/.." || exit 1
export PYTHONUNBUFFERED=1
if [[ ! -x .venv/bin/ava ]]; then
  echo "ava isn't installed yet — run: python3 bootstrap.py"
  exit 1
fi
exec .venv/bin/ava
