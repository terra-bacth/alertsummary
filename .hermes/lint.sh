#!/bin/sh
cd "/Users/mohitsharms/Documents/Agent Builds/Troubleshooting_Agent"
unset PYTHONPATH
.venv/bin/pytest -q 2>&1 | tail -2
.venv/bin/ruff check src tests && echo LINT_OK
