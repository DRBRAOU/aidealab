@echo off
cd /d "%~dp0"
if not exist venv (
  echo First run: installing... this takes a minute.
  python -m venv venv
  call venv\Scripts\activate
  pip install -r requirements.txt
) else (
  call venv\Scripts\activate
)
python server.py %*
pause
