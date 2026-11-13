@echo off
rem Refresh the 13F dashboard and publish it to GitHub Pages. Run from Task Scheduler or by double-click.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python update.py --publish %* >> update.log 2>&1
