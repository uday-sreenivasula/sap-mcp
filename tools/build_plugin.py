"""Build the SAP plugins from plugin/, plugin-write/ and src/sap_mcp.

    .venv\\Scripts\\python tools\\build_plugin.py            # both
    .venv\\Scripts\\python tools\\build_plugin.py read       # ..\\sap-plugin (publish to the org library)
    .venv\\Scripts\\python tools\\build_plugin.py write      # ..\\sap-private-marketplace\\sap-write

The read-only "sap" plugin is built WITHOUT the write modules, so it cannot
write even if someone tried. "sap-write" is private: it goes into a local
marketplace folder that selected users add with /plugin marketplace add.
Plugin versions in the manifests and launch.cmd must equal sap_mcp.__version__.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "sap_mcp"
PLUGIN = REPO / "plugin"
PLUGIN_WRITE = REPO / "plugin-write"
WRITE_ONLY = ("write_client.py", "write_confirm.py", "write_server.py")
MARKETPLACE = "sap-private"


def _version() -> str:
    return re.search(r'__version__\s*=\s*"([^"]+)"', (SRC / "__init__.py").read_text()).group(1)


def _check(version: str) -> list[str]:
    problems = []
    for folder in (PLUGIN, PLUGIN_WRITE):
        if not (folder / ".claude-plugin" / "plugin.json").is_file():
            continue  # sap-write lives outside the public repo
        v = json.loads((folder / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        if v != version:
            problems.append(f"{folder.name}/plugin.json version {v} != sap_mcp {version}")
    if f'set "VER={version}"' not in (PLUGIN / "launch.cmd").read_text(encoding="utf-8"):
        problems.append(f"plugin/launch.cmd VER is not {version}")
    if f'VER="{version}"' not in (PLUGIN / "launch.sh").read_text(encoding="utf-8"):
        problems.append(f"plugin/launch.sh VER is not {version}")
    return problems


def _unix_scripts(folder: Path) -> None:
    """macOS launchers: LF line endings (sh chokes on CR). They are run as
    "/bin/sh launch.sh", so the executable bit is not needed."""
    for sh in folder.glob("*.sh"):
        sh.write_bytes(sh.read_bytes().replace(bytes([13, 10]), bytes([10])))


def _copy_site(pkg: Path) -> None:
    """The company's SAP systems (site.json at the repo root, git-ignored) go
    into the built plugin, so users need not type server addresses. Without it
    the build still works; users then enter the address in the login window."""
    site = REPO / "site.json"
    if site.is_file():
        shutil.copy2(site, pkg / "site.json")
        print(f"  site settings: {site.name} included")
    else:
        print("  site settings: none (site.json missing) - users will be asked for the server")


def _copy_src(dest: Path, include_write: bool) -> None:
    skip = ["__pycache__", "*.pyc", "site.json"] + ([] if include_write else list(WRITE_ONLY))
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns(*skip))


def build_read(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(PLUGIN, out, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    _copy_src(out / "server" / "sap_mcp", include_write=False)
    _copy_site(out / "server" / "sap_mcp")
    _unix_scripts(out)
    leaked = [p.name for p in (out / "server" / "sap_mcp").glob("write_*.py")]
    assert not leaked, f"write modules leaked into the read-only plugin: {leaked}"
    return out


def build_write(market: Path) -> Path:
    out = market / "sap-write"
    if market.exists():
        shutil.rmtree(market)
    out.mkdir(parents=True)
    # Launchers come from the read plugin, pointed at their own data folder and server.
    for name in ("launch.cmd", "setup.cmd", "launch.sh", "setup.sh"):
        text = (PLUGIN / name).read_text(encoding="utf-8")
        if name == "launch.cmd":
            text = text.replace('set "NAME=sap-mcp-plugin"', 'set "NAME=sap-mcp-write-plugin"')
        if name == "launch.sh":
            text = text.replace('NAME="sap-mcp-plugin"', 'NAME="sap-mcp-write-plugin"')
        (out / name).write_text(text, encoding="utf-8")
    shutil.copytree(PLUGIN_WRITE, out, dirs_exist_ok=True)
    (out / "server").mkdir(exist_ok=True)
    run = (PLUGIN / "server" / "run.py").read_text(encoding="utf-8")
    run = run.replace('SERVER_MODULE = "sap_mcp.server"', 'SERVER_MODULE = "sap_mcp.write_server"')
    run = run.replace('"sap-mcp-plugin", "lib"', '"sap-mcp-write-plugin", "lib"')
    (out / "server" / "run.py").write_text(run, encoding="utf-8")
    _copy_src(out / "server" / "sap_mcp", include_write=True)
    _copy_site(out / "server" / "sap_mcp")
    _unix_scripts(out)

    manifest = json.loads((PLUGIN_WRITE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    (market / ".claude-plugin").mkdir()
    (market / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": MARKETPLACE,
        "owner": {"name": "SAP connector for Claude"},
        "plugins": [{"name": "sap-write", "source": "./sap-write",
                     "description": manifest["description"], "version": manifest["version"]}],
    }, indent=2), encoding="utf-8")
    return out


def main() -> int:
    # sap-write is parked (owner decision, Nov 2026): build it only on request.
    which = sys.argv[1] if len(sys.argv) > 1 else "read"
    version = _version()
    problems = _check(version)
    if problems:
        print("Version mismatch:\n  " + "\n  ".join(problems))
        return 1
    if which in ("read", "both"):
        out = build_read(REPO.parent / "sap-plugin")
        print(f"Built sap {version} -> {out} ({sum(1 for p in out.rglob('*') if p.is_file())} files)")
    if which in ("write", "both"):
        out = build_write(REPO.parent / "sap-private-marketplace")
        print(f"Built sap-write {version} -> {out} ({sum(1 for p in out.rglob('*') if p.is_file())} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
