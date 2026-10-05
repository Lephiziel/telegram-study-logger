#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
"${TGSTUDY_PYTHON:-python3}" install.py
