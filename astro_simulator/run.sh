#!/bin/sh
# Launch the interactive dish simulator with default parameters.
# Any arguments are passed through, e.g.  ./run.sh --tsys 100 --nchan 1024
cd "$(dirname "$0")" || exit 1
exec python3 astro_simulator.py "$@"
