@echo off
rem Launch the interactive dish simulator with default parameters.
rem Any arguments are passed through, e.g.  run.bat --tsys 100 --nchan 1024
cd /d "%~dp0"
python astro_simulator.py %*
