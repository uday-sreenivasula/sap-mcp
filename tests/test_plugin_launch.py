"""The plugin's start line must work on Windows (cmd.exe) and macOS (/bin/sh).

cmd.exe scans its whole command line for switches, and Claude passes the
plugin folder with forward slashes (C:/Users/.../Claude-Data/...). Any "/x"
before the real "/c" can be taken as a switch: "/Claude-Data" became "/C" and
cmd tried to run "laude-Data" (0.7.4/0.7.5 outage). So nothing before "/c" may
contain a slash or the plugin path.
"""
import json
from pathlib import Path

MCP = Path(__file__).resolve().parents[1] / "plugin" / ".mcp.json"


def test_start_line_is_safe_for_cmd_and_sh():
    server = json.loads(MCP.read_text(encoding="utf-8"))["mcpServers"]["mcp"]
    assert server["command"] == "${ComSpec:-/bin/sh}"
    args = server["args"]
    i = args.index("/c")
    for token in args[:i]:
        assert "/" not in token and "CLAUDE_PLUGIN_ROOT" not in token, token
    assert args[:i][0] == "-c"                       # sh runs the next string
    assert args[i + 1].endswith("launch.cmd")         # cmd runs launch.cmd
    assert server["env"]["SAP_MCP_LAUNCH"] == "${CLAUDE_PLUGIN_ROOT}/launch.sh"
