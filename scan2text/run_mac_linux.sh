#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -d venv ]; then
  echo "First run: installing... this takes a minute."
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
else
  source venv/bin/activate
fi
python server.py "$@"
