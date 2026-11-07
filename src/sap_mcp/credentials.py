"""Per-user credential storage for sap-mcp.

This is what turns the server multi-user. Instead of one shared SAP login baked
into the Claude Desktop config, every person runs ``sap-mcp setup`` once on their
own machine and logs in with their *own* SAP user. Every OData call from their
client then executes under their own SAP authorizations, so SAP's own security
(S_SERVICE, the data-level auth objects, etc.) decides what they can see — no
mapping to maintain on our side.

Logins are kept **per environment** (``prod``, ``dev`` — see
:mod:`sap_mcp.environments`), because the same person usually has a different
password, and sometimes a different user, on each SAP system.

Where things are stored
-----------------------
* Non-secret settings (base_url, client, username, verify_tls, service_root,
  timeout) live in ``~/.sap-mcp/config.json``::

      {
        "version": 2,
        "environments": {
          "prod": {"base_url": "...", "client": "100", "username": "...", ...},
          "dev":  {"base_url": "...", "client": "200", "username": "...", ...}
        }
      }

  Files written by older versions (a single flat object, production only) are
  still read — they are treated as the ``prod`` entry and rewritten in the new
  shape the next time anything is saved.
* The **password** goes into the OS keychain via the ``keyring`` package when a
  working backend is available:
    - Windows  -> Credential Manager
    - macOS    -> Keychain
    - Linux    -> Secret Service
  Production keeps the original keychain entry name (``sap-mcp``) so existing
  logins keep working; other environments use ``sap-mcp:<env>``.
  If keyring is not installed or has no usable backend, the password falls back
  to the same JSON file (created with 0600 permissions).

Override the storage directory with the ``SAP_MCP_HOME`` environment variable
(handy for tests / throwaway profiles).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from . import environments as envs

# keyring entry: service name per environment, the SAP username is the "account".
KEYRING_SERVICE = "sap-mcp"
_HOME_ENV = "SAP_MCP_HOME"
_FILE_VERSION = 2

# Keys we persist per environment (everything except the password).
_SETTING_KEYS = (
    "base_url",
    "client",
    "username",
    "verify_tls",
    "service_root",
    "timeout_seconds",
)


# --------------------------------------------------------------------- paths

def config_dir() -> Path:
    """Directory that holds this user's sap-mcp config."""
    override = os.environ.get(_HOME_ENV)
    return Path(override) if override else (Path.home() / ".sap-mcp")


def config_file() -> Path:
    return config_dir() / "config.json"


# ------------------------------------------------------------------- keyring

def keyring_service(env: str = envs.DEFAULT_ENV) -> str:
    """Keychain service name for ``env``.

    Production keeps the bare name used before environments existed, so a
    password saved by an older version is still found.
    """
    return KEYRING_SERVICE if envs.is_production(env) else f"{KEYRING_SERVICE}:{env}"


def _keyring():
    """Return the ``keyring`` module if it has a *usable* backend, else None.

    keyring always imports, even on a box with no secret store — it just falls
    back to a null backend that raises on use. We detect that case here so the
    caller can transparently use the file fallback instead.
    """
    try:
        import keyring  # type: ignore

        backend = keyring.get_keyring()
        name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
        if "fail" in name or "null" in name:
            return None
        return keyring
    except Exception:
        return None


def _keyring_set(username: str, password: str, env: str = envs.DEFAULT_ENV) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(keyring_service(env), username, password)
        return True
    except Exception:
        return False


def _keyring_get(username: str, env: str = envs.DEFAULT_ENV) -> Optional[str]:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(keyring_service(env), username)
    except Exception:
        return None


def _keyring_delete(username: str, env: str = envs.DEFAULT_ENV) -> None:
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(keyring_service(env), username)
    except Exception:
        pass


# ------------------------------------------------------------ file handling

def _read_file() -> dict[str, Any]:
    """Return the config file as ``{"version": 2, "environments": {...}}``.

    Upgrades the pre-environments flat layout in memory (as ``prod``). Returns
    an empty structure when there is no usable file.
    """
    empty: dict[str, Any] = {"version": _FILE_VERSION, "environments": {}}
    path = config_file()
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty

    if isinstance(data.get("environments"), dict):
        data.setdefault("version", _FILE_VERSION)
        return data

    # Legacy v1 file: one flat production login.
    if any(k in data for k in ("username", "base_url", "client")):
        return {"version": _FILE_VERSION, "environments": {envs.DEFAULT_ENV: data}}
    return empty


def _write_file(data: dict[str, Any]) -> None:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = config_file()
    # Write then tighten permissions (0600). chmod is a near no-op on Windows
    # but the per-user profile directory is already access-controlled there.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


# -------------------------------------------------------------------- public

def keyring_available() -> bool:
    """True if a real OS keychain backend is usable on this machine."""
    return _keyring() is not None


def save_credentials(settings: dict[str, Any], password: str, env: str = envs.DEFAULT_ENV) -> str:
    """Persist this user's login for one environment.

    ``settings`` must contain at least ``base_url``, ``client`` and
    ``username``. Logins for other environments are left untouched. The
    password is stored in the OS keychain when possible, otherwise in the
    config file.

    Returns the password backend actually used: ``"keyring"`` or ``"file"``.
    """
    username = (settings.get("username") or "").strip()
    if not username:
        raise ValueError("username is required to save credentials")

    data = _read_file()
    all_envs: dict[str, Any] = data["environments"]

    # If the user name changed, drop the old keychain entry for this env.
    previous = all_envs.get(env) or {}
    if previous.get("username") and previous.get("username") != username:
        _keyring_delete(previous["username"], env)

    entry: dict[str, Any] = {k: settings.get(k) for k in _SETTING_KEYS if settings.get(k) is not None}
    backend = "keyring" if _keyring_set(username, password, env) else "file"
    entry["password_backend"] = backend
    if backend == "file":
        # No OS keychain available — keep the password in the 0600 file.
        entry["password"] = password

    all_envs[env] = entry
    data["version"] = _FILE_VERSION
    _write_file(data)
    return backend


def load_stored(env: str = envs.DEFAULT_ENV) -> Optional[dict[str, Any]]:
    """Load one environment's stored settings + password, or None.

    The returned dict includes a ``password`` key (possibly ``None`` if the
    keychain entry has gone missing) and a ``password_backend`` marker.
    """
    entry = _read_file()["environments"].get(env)
    if not isinstance(entry, dict) or not entry:
        return None
    entry = dict(entry)
    if entry.get("password_backend", "file") == "keyring":
        entry["password"] = _keyring_get(entry.get("username", "") or "", env)
    # else: password (if any) is already inline in the file.
    return entry


def stored_environments() -> list[str]:
    """Environments this user has saved a login for."""
    return sorted(_read_file()["environments"])


def clear_credentials(env: Optional[str] = None) -> bool:
    """Remove stored credentials.

    ``env=None`` removes every environment (keychain entries + config file);
    otherwise only that environment's login is removed.

    Returns True if anything was removed.
    """
    data = _read_file()
    all_envs: dict[str, Any] = data["environments"]
    targets = list(all_envs) if env is None else [env]

    removed = False
    for name in targets:
        entry = all_envs.pop(name, None)
        if entry is None:
            continue
        removed = True
        if entry.get("username"):
            _keyring_delete(entry["username"], name)

    path = config_file()
    if not all_envs:
        if path.exists():
            try:
                path.unlink()
                removed = True
            except OSError:
                pass
    elif removed:
        _write_file(data)
    return removed


def status() -> dict[str, Any]:
    """Non-secret summary of how credentials are configured (for diagnostics)."""
    per_env: dict[str, Any] = {}
    names = envs.names() + [e for e in stored_environments() if e not in envs.names()]
    for name in names:
        stored = load_stored(name)
        per_env[name] = {
            "system": envs.describe(name),
            "configured": bool(stored),
            "username": (stored or {}).get("username"),
            "base_url": (stored or {}).get("base_url"),
            "client": (stored or {}).get("client"),
            "password_backend": (stored or {}).get("password_backend"),
            "password_present": bool((stored or {}).get("password")),
        }
    return {
        "config_file": str(config_file()),
        "keyring_available": keyring_available(),
        "environments": per_env,
    }
