"""Shared fixtures.

Every test gets its own empty ``SAP_MCP_HOME`` and export folders, so nothing
reads the real user's stored login or writes into their ``~/.sap-mcp`` or
Downloads folders, and caches from one test cannot leak into the next.
No test may open a window on the user's screen: the allowance prompt
answers "no" unless a test says otherwise.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# Example SAP systems for every test (environments.py reads this at import).
os.environ["SAP_MCP_SITE"] = str(Path(__file__).with_name("site.test.json"))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAP_MCP_HOME", str(tmp_path / "sap-mcp-home"))
    monkeypatch.setenv("SAP_MCP_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("SAP_MCP_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("SAP_MCP_ATTACHMENTS_DIR", str(tmp_path / "attachments"))


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """Passwords in tests go to an in-memory keychain, never to Windows
    Credential Manager. Tests that install their own fake via
    credentials._keyring override this one."""
    import types
    from sap_mcp import credentials
    store: dict = {}
    fake = types.SimpleNamespace(
        set_password=lambda svc, user, pw: store.__setitem__((svc, user), pw),
        get_password=lambda svc, user: store.get((svc, user)),
        delete_password=lambda svc, user: store.pop((svc, user), None),
    )
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)


@pytest.fixture(autouse=True)
def _no_windows(monkeypatch):
    import sap_mcp.usage as usage
    monkeypatch.setattr(usage, "ask_to_continue", lambda state: False)
    try:
        import sap_mcp.server as srv
        srv._extensions = 0
        srv._spent_chars = 0
        srv._call_count = 0
        srv._by_tool.clear()
    except ImportError:
        pass
