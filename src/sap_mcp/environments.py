"""The SAP systems this server can talk to.

sap-mcp started out production-only. It now knows about several named
*environments* (``prod``, ``dev``); each has its own Gateway host, client and
its own stored login, so you can run a query against Development without
touching your Production credentials.

Picking an environment
----------------------
* When the server starts, it uses ``SAP_ENV`` from the Claude Desktop config,
  or ``prod`` if that isn't set.
* During a conversation Claude can call ``switch_environment("dev")`` /
  ``switch_environment("prod")``. Every response from a non-production system
  carries a ``_sap_env`` marker so dev data is never mistaken for live figures.

Logging in
----------
Each environment has its own login::

    sap-mcp setup               # Production (same as before)
    sap-mcp setup --env dev     # Development / Testing

(or, inside Claude, the ``sap_login`` tool, which opens a login window).

Your SAP systems (site settings)
--------------------------------
The code ships with NO server addresses. Each company describes its own
systems in a ``site.json`` (see ``site.example.json``), looked up in this order:

1. the file named by ``SAP_MCP_SITE``;
2. ``site.json`` next to this package (the plugin build puts it there);
3. ``~/.sap-mcp/site.json``.

Any single setting can also be overridden with ``SAP_<ENV>_<FIELD>``
environment variables, e.g. ``SAP_PROD_BASE_URL`` or ``SAP_DEV_CLIENT``. If a
system's address is still unknown, the login window asks for it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: Environment the server starts in when ``SAP_ENV`` is not set. Kept as
#: production so an existing install behaves exactly as it did before.
DEFAULT_ENV = "prod"

#: The environments the server knows. Addresses and clients come from the
#: site settings (see the module docstring); ``base_url`` is the ICM HTTPS
#: endpoint (e.g. https://host:443NN), ``client`` the mandt people log into.
ENVIRONMENTS: dict[str, dict[str, Any]] = {
    "prod": {"label": "Production", "sid": "", "base_url": "", "client": "", "verify_tls": True},
    "dev": {"label": "Development / Testing", "sid": "", "base_url": "", "client": "", "verify_tls": True},
}

_SITE_FIELDS = ("label", "sid", "base_url", "client", "verify_tls")


def site_file() -> Path | None:
    """The site settings file in use, if any."""
    candidates = [os.environ.get("SAP_MCP_SITE") or "",
                  str(Path(__file__).resolve().parent / "site.json"),
                  str(Path.home() / ".sap-mcp" / "site.json")]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def _load_site() -> dict[str, Any]:
    path = site_file()
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: Everything in the site file (environments, company note, default charts).
SITE: dict[str, Any] = _load_site()
for _env, _vals in (SITE.get("environments") or {}).items():
    if not isinstance(_vals, dict):
        continue
    _key = str(_env).strip().lower()
    # prod and dev always exist; a site may add more (e.g. "golden": the
    # development system's customizing client, where repository changes are allowed).
    ENVIRONMENTS.setdefault(_key, {"label": _key, "sid": "", "base_url": "", "client": "", "verify_tls": True})
    ENVIRONMENTS[_key].update({k: v for k, v in _vals.items() if k in _SITE_FIELDS})


def site_value(key: str, default: Any = None) -> Any:
    """A top-level site setting, e.g. ``company_note`` or ``charts``."""
    return SITE.get(key, default)


#: Friendly names people (or Claude) are likely to type.
_ALIASES = {
    "prod": "prod", "prd": "prod", "production": "prod", "live": "prod", "p": "prod",
    "dev": "dev", "development": "dev", "test": "dev", "testing": "dev", "d": "dev",
}
# Site-defined aliases, e.g. {"customizing": "golden", "100": "golden"}.
for _alias, _target in (SITE.get("aliases") or {}).items():
    if str(_target).lower() in ENVIRONMENTS:
        _ALIASES[str(_alias).strip().lower()] = str(_target).lower()


class UnknownEnvironment(ValueError):
    """Raised for an environment name we don't recognise."""


def names() -> list[str]:
    return list(ENVIRONMENTS)


def normalize(name: Any) -> str:
    """Map user input ("Production", "DEV", "test") to a canonical env key."""
    raw = str(name or "").strip().lower()
    if not raw:
        return DEFAULT_ENV
    if raw in ENVIRONMENTS:
        return raw
    if raw in _ALIASES:
        return _ALIASES[raw]
    for key, meta in ENVIRONMENTS.items():
        if raw == str(meta.get("sid", "")).lower():
            return key
    raise UnknownEnvironment(
        f"Unknown SAP environment {name!r}. Choose one of: {', '.join(ENVIRONMENTS)}."
    )


def startup_env() -> str:
    """The environment the server starts in (``SAP_ENV`` or the default)."""
    try:
        return normalize(os.environ.get("SAP_ENV") or DEFAULT_ENV)
    except UnknownEnvironment:
        return DEFAULT_ENV


def meta(env: str) -> dict[str, Any]:
    """Built-in settings for ``env`` (label, SID, host, client, TLS)."""
    return dict(ENVIRONMENTS.get(env, {"label": env, "sid": ""}))


def describe(env: str) -> str:
    """Human label, e.g. ``Development / Testing (DEV)``."""
    m = meta(env)
    sid = m.get("sid")
    return f"{m.get('label', env)} ({sid})" if sid else str(m.get("label", env))


def is_production(env: str) -> bool:
    return env == "prod"


def env_var(env: str, field: str) -> str | None:
    """Look up an environment-variable override for one setting.

    ``SAP_<ENV>_<FIELD>`` (e.g. ``SAP_DEV_CLIENT``) always wins. The original
    un-prefixed ``SAP_<FIELD>`` variables (``SAP_BASE_URL``, ``SAP_USERNAME``,
    ...) still work, but only for **production** — they predate environments
    and existing Claude Desktop configs pin the production host with them, so
    applying them to dev would silently send "dev" queries to production.
    """
    val = os.environ.get(f"SAP_{env.upper()}_{field.upper()}")
    if val not in (None, ""):
        return val
    if is_production(env):
        val = os.environ.get(f"SAP_{field.upper()}")
        if val not in (None, ""):
            return val
    return None
