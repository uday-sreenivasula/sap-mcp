"""Interactive first-run setup for sap-mcp.

Run once per user and per SAP system, in a terminal (NOT by the MCP host):

    sap-mcp setup               # Production
    sap-mcp setup --env dev     # Development / Testing
    # or, without the console script on PATH:
    python -m sap_mcp.server setup [--env dev]

It prompts for the SAP host/client (pre-filled with sensible defaults) and the
person's own SAP user + password, optionally verifies the login against the
live Gateway, and stores the credentials for this user only (OS keychain +
``~/.sap-mcp/config.json``). After that, Claude Desktop launches the server with
no credentials in its config - each person connects as themselves.

Why this is a separate command and not part of server start-up: the MCP server
speaks JSON-RPC over stdin/stdout, so it cannot also prompt on the console.
Setup is therefore a plain CLI you run yourself, once.
"""
from __future__ import annotations

import getpass
from typing import Optional, Sequence

from . import credentials
from . import environments as envs
from .config import SapConfig
from .sap_client import SapODataClient, SapODataError

# Company defaults now live per environment in environments.ENVIRONMENTS, so
# most users only have to type their SAP user name and password. Override with
# --advanced at the prompt, or by exporting SAP_<ENV>_BASE_URL / _CLIENT /
# _VERIFY_TLS (plain SAP_BASE_URL etc. still work for production).
DEFAULT_BASE_URL = envs.ENVIRONMENTS[envs.DEFAULT_ENV]["base_url"]
DEFAULT_CLIENT = envs.ENVIRONMENTS[envs.DEFAULT_ENV]["client"]
DEFAULT_VERIFY_TLS = envs.ENVIRONMENTS[envs.DEFAULT_ENV]["verify_tls"]


def parse_env_arg(args: Sequence[str], default: Optional[str] = envs.DEFAULT_ENV) -> Optional[str]:
    """Pull the environment out of CLI args.

    Accepts ``--env dev``, ``--env=dev`` or a bare ``dev`` / ``prod``. Returns
    ``default`` when none is given. Raises UnknownEnvironment for a bad name.
    """
    args = list(args)
    for i, a in enumerate(args):
        if a == "--env" and i + 1 < len(args):
            return envs.normalize(args[i + 1])
        if a.startswith("--env="):
            return envs.normalize(a.split("=", 1)[1])
    for a in args:
        if not a.startswith("-"):
            return envs.normalize(a)
    return default


def _ask(prompt: str, default: Optional[str] = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        val = input(f"{prompt}{suffix}: ").strip()
        if val:
            return val
        if default not in (None, ""):
            return str(default)
        print("  (required - please enter a value)")


def _ask_bool(prompt: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"{prompt} [{d}]: ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes", "true", "1")


def _verify_login(cfg: SapConfig) -> tuple[bool, str]:
    """Best-effort live check. Returns (ok, human_message).

    ``ok`` is True when SAP accepted the credentials (even a 403 means auth
    succeeded but the user lacks the catalog role - the login itself is valid).
    A 401 means the user/password/client is wrong.
    """
    try:
        with SapODataClient(cfg) as c:
            c.get(
                "/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection",
                params={"$top": 1},
            )
        return True, "SAP accepted the login and the Gateway catalog is reachable."
    except SapODataError as e:
        if e.status_code == 401:
            return False, "SAP rejected the credentials (HTTP 401). Check user, password and client."
        if e.status_code == 403:
            return True, ("Login OK, but this user has no access to the Gateway catalog (HTTP 403). "
                          "That's fine - your data-level authorizations still apply per service.")
        if e.status_code == 0:
            return False, f"Could not reach SAP ({e}). Check the host/port and your network/VPN."
        return True, f"Login reached SAP (HTTP {e.status_code}). Proceeding."
    except Exception as e:  # noqa: BLE001
        return False, f"Unexpected error contacting SAP: {type(e).__name__}: {e}"


def run_setup(args: Sequence[str] = ()) -> int:
    """Interactive setup. Returns a process exit code (0 = success)."""
    skip_test = "--no-test" in args
    advanced = "--advanced" in args
    try:
        env = parse_env_arg(args) or envs.DEFAULT_ENV
    except envs.UnknownEnvironment as e:
        print(e)
        return 2
    defaults = envs.meta(env)

    existing = credentials.load_stored(env) or {}

    print(f"\n=== sap-mcp setup - {envs.describe(env)} - connect with your own SAP login ===\n")
    print("You only need to enter your SAP user name and password.")
    print("Your login is stored for YOUR computer user only; everyone who uses this")
    print("server logs in as themselves and sees only what their SAP role allows.\n")

    # Host / client / TLS are fixed per environment (environments.py, or an
    # SAP_<ENV>_* override) - we do NOT ask for them. Pass --advanced to override.
    base_url = envs.env_var(env, "base_url") or existing.get("base_url") or defaults.get("base_url")
    client = envs.env_var(env, "client") or existing.get("client") or defaults.get("client")

    if advanced or not base_url or not client:
        base_url = _ask("SAP Gateway base URL", base_url)
        client = _ask("SAP client (mandt)", client)
    else:
        print(f"SAP system : {envs.describe(env)}")
        print(f"             {base_url}  (client {client})\n")

    # First login to a second system: suggest the user name from the other
    # environment - it's usually the same SAP user id.
    suggested_user = existing.get("username")
    if not suggested_user:
        for other in credentials.stored_environments():
            if other != env and (credentials.load_stored(other) or {}).get("username"):
                suggested_user = credentials.load_stored(other)["username"]
                break
    username = _ask("Your SAP user name", suggested_user)

    # Password: hidden input. If editing an existing setup, allow keeping the
    # stored one by pressing Enter.
    same_user = (existing.get("username") or "") == username
    have_existing_pw = same_user and bool(existing.get("password"))
    pw_prompt = "Your SAP password"
    if have_existing_pw:
        pw_prompt += " (press Enter to keep the existing one)"
    password = getpass.getpass(pw_prompt + ": ")
    if not password and have_existing_pw:
        password = existing.get("password")

    env_v = envs.env_var(env, "verify_tls")
    if env_v is not None:
        default_verify = env_v.strip().lower() not in ("0", "false", "no")
    elif existing.get("verify_tls") is not None:
        default_verify = existing.get("verify_tls")
    else:
        default_verify = defaults.get("verify_tls", True)
    verify_tls = bool(default_verify)
    if advanced:
        verify_tls = _ask_bool("Verify TLS certificate? (No for self-signed dev systems)", verify_tls)

    if not password:
        print("\nNo password entered - aborting without saving.")
        return 2

    cfg = SapConfig(
        base_url=base_url,
        client=client,
        username=username,
        password=password,
        verify_tls=verify_tls,
        env=env,
    )

    if not skip_test:
        print("\nTesting the login against SAP ...")
        ok, msg = _verify_login(cfg)
        print(f"  {msg}")
        if not ok:
            again = _ask_bool("Save these credentials anyway?", default=False)
            if not again:
                print("Not saved. Run setup again to retry.")
                return 1

    backend = credentials.save_credentials(
        {
            "base_url": base_url,
            "client": client,
            "username": username,
            "verify_tls": verify_tls,
        },
        password,
        env=env,
    )

    print(f"\nSaved login for {envs.describe(env)}.")
    print(f"  Settings file : {credentials.config_file()}")
    if backend == "keyring":
        print("  Password      : stored in your OS keychain (not on disk).")
    else:
        print("  Password      : stored in the settings file above (0600); no OS keychain was available.")
    print("\nYou are logged in. Next:")
    print("  1. Open Claude Desktop (fully quit and reopen it if it is already running).")
    if envs.is_production(env):
        print("  2. You can now ask Claude to use the SAP tools (try: \"call whoami\").\n")
    else:
        print(f"  2. Ask Claude: \"switch the sap server to {env}\", then \"call whoami\".")
        print("     Claude starts on Production each time; it switches only when asked.\n")
    return 0


def print_status() -> None:
    """Print non-secret credential status (used by `sap-mcp status`)."""
    import json

    print(json.dumps(credentials.status(), indent=2))
