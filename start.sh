#!/usr/bin/env bash
# Mac / Linux launcher:  bash start.sh
set -e
cd "$(dirname "$0")"
PY=$(command -v python3.12 || command -v python3.13 || command -v python3.11 || command -v python3)
[ -x .venv/bin/python ] || { echo "First run - setting up ..."; "$PY" -m venv .venv; }
if ! cmp -s requirements.txt .venv/requirements.installed; then
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt .venv/requirements.installed
fi
[ -f ~/.streamlit/credentials.toml ] || { mkdir -p ~/.streamlit; printf '[general]\nemail = ""\n' > ~/.streamlit/credentials.toml; }
exec .venv/bin/python -m streamlit run app.py --browser.gatherUsageStats false
