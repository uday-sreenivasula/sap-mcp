"""Configuration loading.

sap-mcp can talk to more than one SAP system — see :mod:`sap_mcp.environments`
for the list (``prod``, ``dev``). Configuration is resolved **per
environment**, from these layers in order of precedence:

1. **Environment variables.** ``SAP_<ENV>_<FIELD>`` (``SAP_DEV_BASE_URL``,
   ``SAP_DEV_USERNAME``, ...) for any environment. The original un-prefixed
   ``SAP_BASE_URL`` / ``SAP_USERNAME`` / ... still work for production, so the
   old shared-service-account setup and CI keep working unchanged.
2. **Per-user stored credentials** written by ``sap-mcp setup [--env dev]`` (see
   :mod:`sap_mcp.credentials`). This is what makes the server multi-user: each
   person logs in as themselves, so every OData call runs under their own SAP
   authorizations.

Each field is resolved independently, so you can, for example, pin the host via
an env var in the MCP host config while the username/password come from the
per-user store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import credentials
from . import environments as envs


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class SapConfig:
    base_url: str            # e.g. https://s4host.company.local:44300
    client: str              # SAP client / mandt, e.g. "100"
    username: str
    password: str
    verify_tls: bool = True  # set False ONLY for dev systems with self-signed certs
    timeout_seconds: float = 30.0
    # Default OData service root. S/4HANA uses both /sap/opu/odata/sap/ (v2)
    # and /sap/opu/odata4/sap/ (v4). We default to v2 since most standard
    # services CAs use (GL, AP/AR, materials) are still v2.
    service_root: str = "/sap/opu/odata/sap/"
    # Which SAP system this is (key from environments.ENVIRONMENTS).
    env: str = envs.DEFAULT_ENV

    @property
    def root_url(self) -> str:
        return self.base_url.rstrip("/") + self.service_root


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no")


def _var_name(env: str, field: str) -> str:
    """Env-var name to mention in error messages for ``field``."""
    return f"SAP_{field.upper()}" if envs.is_production(env) else f"SAP_{env.upper()}_{field.upper()}"


def _login_hint(env: str) -> str:
    if envs.is_production(env):
        return ("Call the sap_login tool (it opens a login window for the user's own SAP user), "
                "or run `sap-mcp setup`")
    return (
        f"Call sap_login(env=\"{env}\") (it opens a login window), or run "
        f"`sap-mcp setup --env {env}`, "
        f"to log in to {envs.describe(env)} with your own SAP user"
    )


def load_config(env: Optional[str] = None) -> SapConfig:
    """Resolve the configuration for ``env`` (default: the startup environment)."""
    try:
        env = envs.normalize(env) if env else envs.startup_env()
    except envs.UnknownEnvironment as e:
        raise ConfigError(str(e)) from e

    stored = credentials.load_stored(env) or {}

    def pick(field: str, key: str, default: Any = None) -> Any:
        env_val = envs.env_var(env, field)
        if env_val is not None:
            return env_val
        if stored.get(key) not in (None, ""):
            return stored[key]
        return default

    base_url = pick("base_url", "base_url", envs.meta(env).get("base_url") or None)
    client = pick("client", "client", envs.meta(env).get("client") or None)
    username = pick("username", "username")
    # Password is never read from the project; env var first, else the keychain
    # / file written by `sap-mcp setup`.
    password = envs.env_var(env, "password") or stored.get("password")
    # TLS default comes from the environment table (internal Gateways use
    # self-signed certs), so dropping the old SAP_VERIFY_TLS pin is safe.
    verify_raw = pick("verify_tls", "verify_tls", envs.meta(env).get("verify_tls", True))
    service_root = pick("service_root", "service_root", "/sap/opu/odata/sap/")
    timeout = pick("timeout", "timeout_seconds", "30")

    missing = [
        _var_name(env, field)
        for field, val in (
            ("base_url", base_url),
            ("client", client),
            ("username", username),
            ("password", password),
        )
        if not val
    ]
    if missing:
        raise ConfigError(
            f"Missing SAP credentials for {envs.describe(env)}: " + ", ".join(missing) + ". "
            + _login_hint(env)
            + ", or set the corresponding environment variables."
        )

    return SapConfig(
        base_url=base_url,
        client=str(client),
        username=username,
        password=password,
        verify_tls=_as_bool(verify_raw, default=True),
        timeout_seconds=float(timeout),
        service_root=service_root,
        env=env,
    )


def config_source(env: Optional[str] = None) -> str:
    """Where the *identity* (username/password) is coming from — for diagnostics."""
    env = envs.normalize(env) if env else envs.startup_env()
    if envs.env_var(env, "username") and envs.env_var(env, "password"):
        return "environment"
    if (credentials.load_stored(env) or {}).get("username"):
        return f"stored (sap-mcp setup{'' if envs.is_production(env) else ' --env ' + env})"
    return "unconfigured"


def is_configured(env: str) -> bool:
    """True if ``env`` has everything needed to connect (no SAP call made)."""
    try:
        load_config(env)
        return True
    except ConfigError:
        return False
