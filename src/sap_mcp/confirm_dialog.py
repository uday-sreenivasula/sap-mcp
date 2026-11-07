"""A yes / no window for the person at the screen (runs as its own process).

Reads {"title", "lines", "yes", "no"} as JSON on stdin, prints
{"approved": true|false}. Closing, Escape and 5 minutes without an answer
all count as "no". Used for decisions the model must not take by itself,
such as extending the SAP data allowance of a conversation.
"""
from __future__ import annotations

import json
import sys

TIMEOUT_MS = 5 * 60 * 1000


def run(req: dict) -> bool:
    from tkinter import ttk

    from .ui import center, make_root, title_row

    approved = {"v": False}
    root = make_root("SAP connector")
    frame = ttk.Frame(root, padding=(22, 18, 22, 16))
    frame.grid()
    title_row(frame, req.get("title", "Continue?"))
    for i, line in enumerate(req.get("lines", []), start=1):
        ttk.Label(frame, text=line, wraplength="4.6i", justify="left").grid(
            row=i, column=0, sticky="w", pady=(10 if i == 1 else 4, 0))

    def yes():
        approved["v"] = True
        root.destroy()

    buttons = ttk.Frame(frame)
    buttons.grid(row=len(req.get("lines", [])) + 1, column=0, sticky="e", pady=(18, 0))
    no_text = req.get("no", "No")
    yes_btn = ttk.Button(buttons, text=req.get("yes", "Yes"), command=yes)
    yes_btn.grid(row=0, column=1)
    if no_text is not None:      # "no": None -> an OK-only message
        # The cautious answer is the default: Enter / Space never says yes by accident.
        no_btn = ttk.Button(buttons, text=no_text, command=root.destroy, default="active")
        no_btn.grid(row=0, column=0, padx=(0, 8))
        default = no_btn
        root.bind("<Return>", lambda _e: root.destroy())
    else:
        default = yes_btn
        root.bind("<Return>", lambda _e: yes())
    root.bind("<Escape>", lambda _e: root.destroy())
    root.after(TIMEOUT_MS, root.destroy)
    center(root)
    default.focus_set()
    root.focus_force()
    root.mainloop()
    return approved["v"]


def main() -> None:
    try:
        ok = run(json.loads(sys.stdin.read() or "{}"))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"approved": False, "error": f"{type(e).__name__}: {e}"}))
        return
    print(json.dumps({"approved": ok}))


if __name__ == "__main__":
    main()
