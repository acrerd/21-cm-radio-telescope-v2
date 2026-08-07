@echo off
rem Launch the interactive HI4PI dish simulator with default parameters.
rem Any arguments are passed through, e.g.  run.bat --tsys 100 --nchan 1024
cd /d "%~dp0"
python hi4pi_interactive.py %*
