#!/bin/zsh
# Stop + start. Use this after editing Python code in src/quicklabel/.
set -euo pipefail
HERE="${0:A:h}"
"$HERE/stop.command"
sleep 1
"$HERE/start.command"
