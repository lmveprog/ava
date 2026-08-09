#!/bin/zsh
cd /Users/matheus/Documents/ava || exit 1
export PYTHONUNBUFFERED=1
exec .venv/bin/python ava.py
