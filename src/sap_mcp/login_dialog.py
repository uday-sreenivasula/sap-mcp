"""SAP login window for the plugin build.

The plugin has no installer to type a password into, and a password must
never pass through the chat. So ``sap_login`` opens this small dialog
in its own process: the user types their own SAP user and password, the login
is tested against SAP, and on success it is saved exactly as ``sap-mcp setup``
saves it (settings in ~/.sap-mcp/config.json, password in the OS keychain:
Windows Credential Manager or the macOS Keychain). The parent only ever receives a one-line JSON result, never the
password.

Run as ``python -m sap_mcp.login_dialog <env>``.
"""
from __future__ import annotations

import json
import sys

from . import credentials
from . import environments as envs
from .config import SapConfig
from .setup_cli import _verify_login


def run(env: str) -> dict:
    import tkinter as tk
    from tkinter import ttk

    from .ui import center, make_root, title_row

    env = envs.normalize(env)
    meta = envs.meta(env)
    stored = credentials.load_stored(env) or {}
    base_url = envs.env_var(env, "base_url") or stored.get("base_url") or meta.get("base_url")
    client = envs.env_var(env, "client") or stored.get("client") or meta.get("client")
    verify_tls = stored.get("verify_tls", meta.get("verify_tls", True))
    username = stored.get("username") or next(
        ((credentials.load_stored(o) or {}).get("username")
         for o in credentials.stored_environments() if o != env), ""
    ) or ""

    result: dict = {"ok": False, "env": env, "message": "Login window closed without logging in."}

    root = make_root(f"SAP login - {envs.describe(env)}")
    frame = ttk.Frame(root, padding=(22, 18, 22, 16))
    frame.grid()
    frame.columnconfigure(1, weight=1)

    title_row(frame, f"Log in to {envs.describe(env)}" + (f", client {client}" if client else ""),
              columnspan=2)
    store = ("the macOS Keychain" if sys.platform == "darwin"
             else "Windows Credential Manager")
    ttk.Label(frame, text=f"Use your own SAP user. The password is stored in {store} "
                          "for your computer user only.",
              style="Hint.TLabel", wraplength="4.2i", justify="left").grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(6, 14))

    # No address in the site settings (e.g. a fresh public install): ask for it.
    ask_server = not (base_url and client)
    row = 2
    url_var = tk.StringVar(value=base_url or "https://")
    client_var = tk.StringVar(value=str(client or ""))
    if ask_server:
        ttk.Label(frame, text="SAP server").grid(row=row, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(frame, textvariable=url_var, width=34).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Label(frame, text="e.g. https://sap-prod.example.com:44300 (your Gateway / Fiori address)",
                  style="Hint.TLabel", wraplength="3.6i", justify="left").grid(
            row=row + 1, column=1, sticky="w")
        ttk.Label(frame, text="Client").grid(row=row + 2, column=0, sticky="w", padx=(0, 12))
        ttk.Entry(frame, textvariable=client_var, width=8).grid(row=row + 2, column=1, sticky="w", pady=4)
        row += 3

    ttk.Label(frame, text="SAP user").grid(row=row, column=0, sticky="w", padx=(0, 12))
    user_var = tk.StringVar(value=username)
    user_entry = ttk.Entry(frame, textvariable=user_var, width=34)
    user_entry.grid(row=row, column=1, sticky="ew", pady=4)
    ttk.Label(frame, text="Password").grid(row=row + 1, column=0, sticky="w", padx=(0, 12))
    pw_var = tk.StringVar()
    pw_entry = ttk.Entry(frame, textvariable=pw_var, show="•", width=34)
    pw_entry.grid(row=row + 1, column=1, sticky="ew", pady=4)

    status = ttk.Label(frame, text="", style="Error.TLabel", wraplength="4.2i", justify="left")
    status.grid(row=row + 2, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def submit(_event=None):
        user = user_var.get().strip().upper()
        pw = pw_var.get()
        url = url_var.get().strip().rstrip("/") if ask_server else base_url
        cli = client_var.get().strip() if ask_server else str(client)
        if ask_server and (not url.lower().startswith(("https://", "http://")) or len(url) < 12
                           or not cli.isdigit()):
            status.config(text="Enter the SAP server address (https://host:port) and the "
                               "3-digit client.", style="Error.TLabel")
            return
        if not user or not pw:
            status.config(text="Enter your SAP user and password.", style="Error.TLabel")
            return
        status.config(text="Checking with SAP ...", style="Hint.TLabel")
        root.update_idletasks()
        cfg = SapConfig(base_url=url, client=cli, username=user,
                        password=pw, verify_tls=bool(verify_tls), env=env)
        ok, msg = _verify_login(cfg)
        if not ok:
            status.config(text=msg, style="Error.TLabel")
            pw_var.set("")
            pw_entry.focus_set()
            return
        backend = credentials.save_credentials(
            {"base_url": url, "client": cli, "username": user,
             "verify_tls": bool(verify_tls)},
            pw, env=env,
        )
        result.update(ok=True, username=user, password_store=backend,
                      message=f"Logged in to {envs.describe(env)} as {user}. {msg}")
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=row + 3, column=0, columnspan=2, sticky="e", pady=(16, 0))
    ttk.Button(buttons, text="Cancel", command=root.destroy).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(buttons, text="Log in", command=submit, default="active").grid(row=0, column=1)
    root.bind("<Return>", submit)
    root.bind("<Escape>", lambda _e: root.destroy())

    center(root)
    (pw_entry if username else user_entry).focus_force()
    root.mainloop()
    return result


def main() -> None:
    env = sys.argv[1] if len(sys.argv) > 1 else envs.DEFAULT_ENV
    try:
        out = run(env)
    except Exception as e:  # noqa: BLE001
        out = {"ok": False, "env": env, "message": f"Login window failed: {type(e).__name__}: {e}"}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
