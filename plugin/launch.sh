#!/bin/sh
# macOS twin of launch.cmd: starts the SAP MCP server. First run (or a new plugin
# version, or the Python it was set up with has gone): runs setup.sh, which finds or
# downloads Python and installs the packages. stdout belongs to the MCP protocol, so
# setup output goes to ~/Library/Application Support/<NAME>/setup.log.
# NAME and ENTRY differ between the sap and sap-write plugins (set by tools/build_plugin.py).
ROOT="$(cd "$(dirname "$0")" && pwd)"
NAME="sap-mcp-plugin"
ENTRY="run.py"
VER="0.7.7"
DATA="$HOME/Library/Application Support/$NAME"
mkdir -p "$DATA"

PY=""
[ -f "$DATA/python.txt" ] && PY="$(cat "$DATA/python.txt")"
if [ ! -f "$DATA/ready-$VER" ] || [ -z "$PY" ] || [ ! -x "$PY" ]; then
  if ! /bin/sh "$ROOT/setup.sh" "$VER" "$DATA" "$ENTRY" >> "$DATA/setup.log" 2>&1; then
    echo "SAP connector setup failed - see $DATA/setup.log" 1>&2
    exit 1
  fi
  PY="$(cat "$DATA/python.txt")"
fi

export PYTHONIOENCODING=utf-8
export SAP_MCP_LIB="$DATA/lib-$VER"
exec "$PY" -s "$ROOT/server/$ENTRY"
