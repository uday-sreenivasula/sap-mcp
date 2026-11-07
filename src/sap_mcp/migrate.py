"""Retire the old stand-alone SAP connector when the plugin starts.

Before the plugin, each PC had a "sap" server in Claude Desktop's config that
ran ``python -m sap_mcp.server`` from %LOCALAPPDATA%\\sap-mcp-venv. With both
present, Claude shows every SAP tool twice and may pick the old one. On start,
the plugin:

1. removes exactly those config entries (command or arguments mention
   sap_mcp.server or sap-mcp-venv, or the entry points at the company's SAP
   gateway, e.g. a hand-made local server on a Mac), after backing up the file;
2. stops the old connector's running processes straight away;
3. if it removed an entry (a real switch-over, not a later start): deletes
   the stored SAP login, so the user signs in again through the plugin, and
   opens a window asking them to restart Claude now.

Only runs inside the plugin (run.py sets SAP_MCP_PLUGIN=1): an old-style
install running this code must never remove itself.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional

from . import credentials


def config_paths() -> list[Path]:
    """Every Claude Desktop config this user may have."""
    if sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        return [p] if p.exists() else []
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    if not (local or roaming):
        return []
    patterns = [
        os.path.join(local, "Claude-Data", "*", "claude_desktop_config.json"),          # Store build
        os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
                     "claude_desktop_config.json"),                                     # older Store build
        os.path.join(roaming, "Claude", "claude_desktop_config.json"),                  # installer build
    ]
    seen, out = set(), []
    for pattern in patterns:
        for p in glob.glob(pattern):
            key = os.path.normcase(os.path.abspath(p))
            if key not in seen:
                seen.add(key)
                out.append(Path(p))
    return out


def _sap_hosts() -> set[str]:
    """host:port of the configured SAP gateways (prod, dev)."""
    from urllib.parse import urlparse

    from . import environments as envs
    hosts = set()
    for env in ("prod", "dev"):
        url = (envs.meta(env) or {}).get("base_url") or ""
        if urlparse(url).netloc:
            hosts.add(urlparse(url).netloc.lower())
    return hosts


def _is_old_connector(server: dict) -> bool:
    """Our old stand-alone install (Windows), or any other hand-made local server
    that talks to the company's SAP gateway directly (seen on a Mac: a local "sap"
    server logging in to production with a shared SAP user). Either duplicates the
    plugin's tools and bypasses the per-user login, so it goes."""
    text = json.dumps(server).lower()
    if "sap_mcp.server" in text or "sap-mcp-venv" in text:
        return True
    return any(host in text for host in _sap_hosts())


def remove_old_connector(paths: Optional[Iterable[Path]] = None) -> list[dict]:
    """Remove old SAP entries; returns what was removed (for the log)."""
    removed = []
    for path in paths if paths is not None else config_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        old = [name for name, s in servers.items() if isinstance(s, dict) and _is_old_connector(s)]
        if not old:
            continue
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, path.with_name(f"{path.name}.bak-sap-plugin-{stamp}"))
            for name in old:
                servers.pop(name)
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            continue
        removed.append({"config": str(path), "removed": old})
    if removed:
        try:
            log = credentials.config_dir() / "migration.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": _dt.datetime.now().isoformat(timespec="seconds"),
                                    "removed": removed}) + "\n")
        except OSError:
            pass
    return removed


def stop_old_processes() -> int:
    """End running copies of the old connector (and their child processes).

    Matches only processes started from sap-mcp-venv with sap_mcp.server; the
    plugin runs from its own folder via run.py, so it is never matched.
    """
    if sys.platform != "win32":
        return 0
    script = (
        "$n = 0; Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
        "$_.CommandLine -match 'sap-mcp-venv' -and $_.CommandLine -match 'sap_mcp\\.server' } | "
        "ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null; $n++ }; $n"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
        return int(out.splitlines()[-1]) if out else 0
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return 0


def ask_for_restart() -> None:
    """Window (own process, does not block start-up): restart Claude now."""
    request = {
        "title": "SAP for Claude has been upgraded",
        "lines": [
            "The new SAP plugin has replaced the old SAP connector on this PC, and the old "
            "connector has been stopped.",
            ("Please restart Claude now: press Cmd+Q in Claude, then open Claude again."
             if sys.platform == "darwin" else
             "Please restart Claude now: right-click the Claude icon in the taskbar tray, choose "
             "Quit, then open Claude again."),
            "After the restart, ask Claude to \"log in to SAP\" and sign in with your own SAP user.",
        ],
        "yes": "OK",
        "no": None,
    }
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(x for x in sys.path if x))
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "sap_mcp.confirm_dialog"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        proc.stdin.write(json.dumps(request))
        proc.stdin.close()
    except OSError:
        pass


def retire_old_connector() -> dict:
    """Everything the plugin does about the old connector at start-up."""
    removed = remove_old_connector()
    stopped = stop_old_processes()
    relogin = False
    if removed:
        relogin = credentials.clear_credentials(None)
        ask_for_restart()
    return {"removed": removed, "stopped_processes": stopped, "login_cleared": relogin}
