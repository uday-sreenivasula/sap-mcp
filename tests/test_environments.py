"""Production / Development environment support.

Covers: per-environment credential storage (incl. reading the old flat,
production-only config file), env-var precedence, the switch_environment /
list_environments tools, the non-production response marker, and the setup
CLI's --env handling. SAP itself is mocked with respx.
"""
from __future__ import annotations

import base64
import json
import os

import httpx
import pytest
import respx

PROD_URL = "https://sap-prod.example.com:44300"
DEV_URL = "https://sap-dev.example.com:44300"
CATALOG = "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"


def _call(tool, **kwargs):
    fn = getattr(tool, "fn", tool)
    return fn(**kwargs)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    # Fresh, empty credential store and no SAP_* env vars leaking in.
    monkeypatch.setenv("SAP_MCP_HOME", str(tmp_path))
    for k in list(os.environ):
        if k.startswith("SAP_") and k != "SAP_MCP_HOME":
            monkeypatch.delenv(k, raising=False)
    import sap_mcp.credentials as cred
    monkeypatch.setattr(cred, "_keyring", lambda: None)  # file backend
    import sap_mcp.server as srv
    srv._client = None
    srv._active_env = "prod"
    srv._spent_chars = 0
    srv._call_count = 0
    yield
    srv._client = None
    srv._active_env = "prod"


def _save(env, user="jdoe", pw="secret", base_url=None, client=None):
    from sap_mcp import credentials, environments as envs
    m = envs.meta(env)
    credentials.save_credentials(
        {"base_url": base_url or m["base_url"], "client": client or m["client"],
         "username": user, "verify_tls": False},
        pw, env=env,
    )


# --------------------------------------------------------------- catalogue

def test_environment_catalogue_has_prod_and_dev():
    from sap_mcp import environments as envs
    assert envs.DEFAULT_ENV == "prod"
    assert envs.ENVIRONMENTS["prod"]["base_url"] == PROD_URL
    assert envs.ENVIRONMENTS["prod"]["client"] == "100"
    assert envs.ENVIRONMENTS["dev"]["base_url"] == DEV_URL
    assert envs.ENVIRONMENTS["dev"]["client"] == "200"
    assert envs.ENVIRONMENTS["dev"]["sid"] == "DEV"


def test_normalize_accepts_friendly_names_and_sid():
    from sap_mcp import environments as envs
    for raw in ("dev", "DEV", "Development", "testing", "test", "DEV", " dev "):
        assert envs.normalize(raw) == "dev", raw
    for raw in ("prod", "Production", "PRD", "live", "PRD"):
        assert envs.normalize(raw) == "prod", raw
    with pytest.raises(envs.UnknownEnvironment):
        envs.normalize("quality")


def test_startup_env_follows_SAP_ENV(monkeypatch):
    from sap_mcp import environments as envs
    assert envs.startup_env() == "prod"
    monkeypatch.setenv("SAP_ENV", "development")
    assert envs.startup_env() == "dev"
    monkeypatch.setenv("SAP_ENV", "nonsense")
    assert envs.startup_env() == "prod"  # bad value never strands the server


# ---------------------------------------------------------------- storage

def test_legacy_flat_config_is_read_as_prod(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "base_url": PROD_URL, "client": "100", "username": "jdoe",
        "verify_tls": False, "password_backend": "file", "password": "old",
    }))
    from sap_mcp.config import load_config, ConfigError
    cfg = load_config("prod")
    assert (cfg.base_url, cfg.client, cfg.username, cfg.password, cfg.env) == (
        PROD_URL, "100", "jdoe", "old", "prod")
    with pytest.raises(ConfigError) as ei:
        load_config("dev")
    assert "Development / Testing (DEV)" in str(ei.value)
    assert "--env dev" in str(ei.value)


def test_saving_dev_keeps_legacy_prod_login(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "base_url": PROD_URL, "client": "100", "username": "jdoe",
        "password_backend": "file", "password": "prodpw",
    }))
    _save("dev", user="jdoe", pw="devpw")
    data = json.loads((tmp_path / "config.json").read_text())
    assert data["version"] == 2
    assert set(data["environments"]) == {"prod", "dev"}
    from sap_mcp.config import load_config
    assert load_config("prod").password == "prodpw"
    dev = load_config("dev")
    assert (dev.base_url, dev.client, dev.password) == (DEV_URL, "200", "devpw")


def test_clear_one_environment_keeps_the_other():
    from sap_mcp import credentials
    _save("prod"); _save("dev")
    assert credentials.clear_credentials("dev") is True
    assert credentials.load_stored("dev") is None
    assert credentials.load_stored("prod")["username"] == "jdoe"
    assert credentials.clear_credentials() is True
    assert not credentials.config_file().exists()


def test_keyring_entries_are_separate_per_environment(monkeypatch):
    store = {}

    class FakeKeyring:
        @staticmethod
        def set_password(svc, user, pw): store[(svc, user)] = pw
        @staticmethod
        def get_password(svc, user): return store.get((svc, user))
        @staticmethod
        def delete_password(svc, user): store.pop((svc, user), None)

    import sap_mcp.credentials as cred
    monkeypatch.setattr(cred, "_keyring", lambda: FakeKeyring)
    _save("prod", pw="p1")
    _save("dev", pw="d1")
    # Prod keeps the original service name so old logins still resolve.
    assert store == {("sap-mcp", "jdoe"): "p1", ("sap-mcp:dev", "jdoe"): "d1"}
    raw = json.loads(cred.config_file().read_text())
    assert "password" not in raw["environments"]["prod"]
    assert cred.load_stored("dev")["password"] == "d1"


def test_legacy_flat_config_with_keychain_password_still_works(tmp_path, monkeypatch):
    # What an existing Windows install has today: flat file + Credential Manager.
    (tmp_path / "config.json").write_text(json.dumps({
        "base_url": PROD_URL, "client": "100", "username": "jdoe",
        "verify_tls": False, "password_backend": "keyring",
    }))

    class FakeKeyring:
        @staticmethod
        def get_password(svc, user):
            return "kcpw" if (svc, user) == ("sap-mcp", "jdoe") else None

    import sap_mcp.credentials as cred
    monkeypatch.setattr(cred, "_keyring", lambda: FakeKeyring)
    from sap_mcp.config import load_config
    assert load_config().password == "kcpw"


# ------------------------------------------------------- env-var precedence

def test_legacy_env_vars_apply_to_prod_only(monkeypatch):
    # An existing Claude Desktop config pins the prod host with these.
    monkeypatch.setenv("SAP_BASE_URL", PROD_URL)
    monkeypatch.setenv("SAP_CLIENT", "100")
    _save("prod"); _save("dev")
    from sap_mcp.config import load_config
    assert load_config("prod").base_url == PROD_URL
    dev = load_config("dev")
    assert dev.base_url == DEV_URL and dev.client == "200"


def test_prefixed_env_vars_override_stored(monkeypatch):
    _save("dev")
    monkeypatch.setenv("SAP_DEV_CLIENT", "210")
    monkeypatch.setenv("SAP_DEV_BASE_URL", "https://dev.example:44301")
    from sap_mcp.config import load_config
    cfg = load_config("dev")
    assert (cfg.base_url, cfg.client) == ("https://dev.example:44301", "210")


def test_env_vars_alone_can_configure_dev(monkeypatch):
    for k, v in dict(SAP_DEV_BASE_URL=DEV_URL, SAP_DEV_CLIENT="200",
                     SAP_DEV_USERNAME="SVC", SAP_DEV_PASSWORD="pw").items():
        monkeypatch.setenv(k, v)
    from sap_mcp.config import load_config, config_source
    assert load_config("dev").username == "SVC"
    assert config_source("dev") == "environment"


# ------------------------------------------------------------- MCP tools

def test_whoami_reports_environment():
    _save("prod")
    from sap_mcp.server import whoami
    out = json.loads(_call(whoami))
    assert out["environment"] == "prod"
    assert out["is_production"] is True
    assert out["base_url"] == PROD_URL
    assert "secret" not in json.dumps(out)


def test_switch_to_unconfigured_env_is_refused():
    _save("prod")
    import sap_mcp.server as srv
    out = json.loads(_call(srv.switch_environment, env="dev"))
    assert out["error"] == "not_configured"
    assert 'sap_login(env="dev")' in out["message"]
    assert srv._active_env == "prod"


def test_switch_unknown_env():
    import sap_mcp.server as srv
    out = json.loads(_call(srv.switch_environment, env="qa2"))
    assert out["error"] == "unknown_environment"
    assert srv._active_env == "prod"


def test_list_environments_shows_login_state():
    _save("prod")
    import sap_mcp.server as srv
    out = json.loads(_call(srv.list_environments))
    by = {e["environment"]: e for e in out["environments"]}
    assert out["active_environment"] == "prod"
    assert by["prod"]["configured"] and by["prod"]["active"]
    assert not by["dev"]["configured"]
    assert by["dev"]["base_url"] == DEV_URL and by["dev"]["client"] == "200"
    assert 'sap_login(env="dev")' in by["dev"]["how_to_log_in"]


@respx.mock
def test_switching_routes_queries_and_tags_dev_responses():
    _save("prod", pw="prodpw")
    _save("dev", pw="devpw")
    prod = respx.get(PROD_URL + CATALOG).mock(
        return_value=httpx.Response(200, json={"d": {"results": [{"TechnicalServiceName": "P_SRV"}]}}))
    dev = respx.get(DEV_URL + CATALOG).mock(
        return_value=httpx.Response(200, json={"d": {"results": [{"TechnicalServiceName": "D_SRV"}]}}))

    import sap_mcp.server as srv

    out = json.loads(_call(srv.list_services))
    assert "_sap_env" not in out           # prod output unchanged
    assert prod.called and not dev.called

    sw = json.loads(_call(srv.switch_environment, env="DEV"))
    assert sw["environment"] == "dev" and sw["active"] is True
    assert sw["switched"] is True and sw["previous_environment"] == "prod"

    out = json.loads(_call(srv.list_services))
    assert "NOT production" in out["_sap_env"]
    req = dev.calls.last.request
    assert req.url.params["sap-client"] == "200"
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"jdoe:devpw").decode()

    json.loads(_call(srv.switch_environment, env="prod"))
    out = json.loads(_call(srv.list_services))
    assert "_sap_env" not in out
    assert prod.calls.last.request.url.params["sap-client"] == "100"


@respx.mock
def test_errors_on_dev_are_tagged_too():
    _save("dev")
    respx.get(DEV_URL + CATALOG).mock(return_value=httpx.Response(401, text="no"))
    import sap_mcp.server as srv
    _call(srv.switch_environment, env="dev")
    out = json.loads(_call(srv.list_services))
    assert out["error"] == "sap" and out["status"] == 401
    assert "_sap_env" in out


# ------------------------------------------------------------------ CLI

def test_parse_env_arg():
    from sap_mcp.setup_cli import parse_env_arg
    assert parse_env_arg([]) == "prod"
    assert parse_env_arg(["--env", "dev"]) == "dev"
    assert parse_env_arg(["--no-test", "--env=Testing"]) == "dev"
    assert parse_env_arg(["dev", "--no-test"]) == "dev"
    assert parse_env_arg([], default=None) is None


def test_setup_for_dev_uses_dev_defaults_and_suggests_prod_user(monkeypatch):
    _save("prod", user="jdoe", pw="prodpw")
    import builtins
    import sap_mcp.setup_cli as cli
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return ""  # accept the suggested user name

    monkeypatch.setattr(builtins, "input", fake_input)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "devpw")
    assert cli.run_setup(["--env", "dev", "--no-test"]) == 0
    assert any("[jdoe]" in p for p in prompts)

    from sap_mcp.config import load_config
    dev = load_config("dev")
    assert (dev.base_url, dev.client, dev.username, dev.password, dev.verify_tls) == (
        DEV_URL, "200", "jdoe", "devpw", False)
    assert load_config("prod").password == "prodpw"


# ------------------------------------------------- retiring the old connector

def test_migration_removes_only_the_old_sap_entry(tmp_path):
    import json as _json
    from sap_mcp import migrate
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(_json.dumps({"mcpServers": {
        "sap": {"command": r"C:\Users\x\AppData\Local\sap-mcp-venv\Scripts\python.exe",
                "args": ["-m", "sap_mcp.server"], "env": {"SAP_ENV": "prod"}},
        "tally": {"command": "node", "args": [r"C:\tools\tally-mcp\index.mjs"]},
    }, "other": 1}), encoding="utf-8")
    removed = migrate.remove_old_connector([cfg])
    assert removed == [{"config": str(cfg), "removed": ["sap"]}]
    data = _json.loads(cfg.read_text(encoding="utf-8"))
    assert list(data["mcpServers"]) == ["tally"] and data["other"] == 1
    assert list(tmp_path.glob("claude_desktop_config.json.bak-sap-plugin-*"))
    # Nothing left to do the second time; no new backup.
    assert migrate.remove_old_connector([cfg]) == []
    assert len(list(tmp_path.glob("*.bak-sap-plugin-*"))) == 1


def test_migration_removes_hand_made_server_pointing_at_sap(tmp_path):
    """A local "sap" server on a Mac that logs in to PRD with a shared user."""
    import json as _json
    from sap_mcp import migrate
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(_json.dumps({"mcpServers": {
        "sap": {"command": "/usr/local/bin/python3", "args": ["/Users/s/sap/server.py"],
                "env": {"SAP_BASE_URL": "https://sap-prod.example.com:44300", "SAP_USER": "claude"}},
        "files": {"command": "npx", "args": ["@modelcontextprotocol/server-filesystem", "/Users/s"]},
    }}), encoding="utf-8")
    removed = migrate.remove_old_connector([cfg])
    assert removed == [{"config": str(cfg), "removed": ["sap"]}]
    assert list(_json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]) == ["files"]


def test_migration_ignores_broken_or_missing_configs(tmp_path):
    from sap_mcp import migrate
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert migrate.remove_old_connector([bad, tmp_path / "missing.json"]) == []


def test_switch_over_stops_old_clears_login_and_asks_for_restart(tmp_path, monkeypatch):
    import json as _json
    from sap_mcp import credentials, migrate
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(_json.dumps({"mcpServers": {"sap": {
        "command": r"C:\Users\x\AppData\Local\sap-mcp-venv\Scripts\python.exe",
        "args": ["-m", "sap_mcp.server"]}}}), encoding="utf-8")
    credentials.save_credentials({"base_url": "https://h", "client": "100", "username": "U1"}, "pw")
    calls = []
    monkeypatch.setattr(migrate, "config_paths", lambda: [cfg])
    monkeypatch.setattr(migrate, "stop_old_processes", lambda: calls.append("stop") or 2)
    monkeypatch.setattr(migrate, "ask_for_restart", lambda: calls.append("restart"))
    out = migrate.retire_old_connector()
    assert out["removed"] and out["stopped_processes"] == 2 and out["login_cleared"] is True
    assert calls == ["stop", "restart"]
    assert credentials.load_stored("prod") is None          # must log in again
    # A later start: nothing to remove, no new restart prompt, login untouched.
    credentials.save_credentials({"base_url": "https://h", "client": "100", "username": "U1"}, "pw")
    calls.clear()
    out = migrate.retire_old_connector()
    assert out["removed"] == [] and calls == ["stop"]
    assert credentials.load_stored("prod")["username"] == "U1"
