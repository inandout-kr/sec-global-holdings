@echo off
rem Refresh the 13F dashboard, send Telegram alerts for new filings, and publish to GitHub Pages.
rem Runs 3 times a day from Task Scheduler (08:30, 12:30, 23:00 KST).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python update.py --notify --publish %* >> update.log 2>&1
