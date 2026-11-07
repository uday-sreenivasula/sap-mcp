"""Shared look for the connector's small windows (login, allowance, write confirmation).

Without DPI awareness Windows draws Tk windows at 96 dpi and stretches them on
scaled displays (125%, 150%), which looks blurry. This marks the process as
per-monitor DPI aware before Tk starts, scales Tk to the real DPI, and uses
the native Windows theme with Segoe UI. On macOS Tk is already Retina-aware;
there the native "aqua" theme and the system font are used, and the window is
brought to the front (a process started by Claude would otherwise open behind it).
"""
from __future__ import annotations

import sys
from pathlib import Path

MAC = sys.platform == "darwin"
FONT = "SF Pro Text" if MAC else "Segoe UI"
TITLE_FONT = (FONT, 15, "bold") if MAC else ("Segoe UI Semibold", 12)
BASE_SIZE = 13 if MAC else 10
ASSETS = Path(__file__).resolve().parent / "assets"


def _dpi_aware() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor (Windows 8.1+)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()     # older Windows
        except (AttributeError, OSError):
            pass


def make_root(title: str):
    """A crisp, native-looking Tk root window."""
    _dpi_aware()
    import tkinter as tk
    from tkinter import font, ttk

    root = tk.Tk()
    root.title(title)
    # Tk sizes fonts in points; tell it the real pixels per point.
    if not MAC:  # macOS Tk already maps points to the screen correctly
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    style = ttk.Style(root)
    for theme in ("aqua", "vista", "winnative", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    families = set(font.families(root))
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            if FONT in families:
                font.nametofont(name).configure(family=FONT, size=BASE_SIZE)
            else:
                font.nametofont(name).configure(size=BASE_SIZE)
        except tk.TclError:
            pass
    style.configure("Title.TLabel", font=TITLE_FONT if TITLE_FONT[0] in families
                    else ("TkDefaultFont", TITLE_FONT[1], "bold"))
    style.configure("Hint.TLabel", foreground="#5f6368")
    style.configure("Error.TLabel", foreground="#b00020")
    if not MAC:  # aqua buttons have a fixed native look
        style.configure("TButton", padding=(14, 5))
    # SAP badge instead of Tk's default feather icon (plain blue if missing).
    try:
        icons = [tk.PhotoImage(file=str(ASSETS / f"sap_badge_{n}.png")) for n in (64, 32)]
    except tk.TclError:
        square = tk.PhotoImage(width=16, height=16)
        square.put("#0a6ed1", to=(0, 0, 16, 16))
        icons = [square]
    root.iconphoto(True, *icons)
    root._sap_icons = icons  # keep references so Tk does not drop the images
    root.resizable(False, False)
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    if MAC:
        _bring_to_front()
    return root


def _bring_to_front() -> None:
    """macOS: activate this Python process so its window is not hidden behind Claude.

    Calls [NSApp activateIgnoringOtherApps:YES] through ctypes: no extra
    packages and, unlike osascript/System Events, no permission prompt.
    """
    import ctypes
    from ctypes import c_bool, c_void_p

    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype = c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send0 = ctypes.CFUNCTYPE(c_void_p, c_void_p, c_void_p)(
            ctypes.cast(objc.objc_msgSend, c_void_p).value)
        send1 = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, c_bool)(
            ctypes.cast(objc.objc_msgSend, c_void_p).value)
        app = send0(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        if app:
            send1(app, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
    except (OSError, AttributeError, ValueError):
        pass


def title_row(parent, text: str, row: int = 0, columnspan: int = 1):
    """SAP badge + bold title, as the first row of a dialog."""
    import tkinter as tk
    from tkinter import ttk

    box = ttk.Frame(parent)
    box.grid(row=row, column=0, columnspan=columnspan, sticky="w")
    try:
        badge = tk.PhotoImage(file=str(ASSETS / "sap_badge_48.png"))
        label = ttk.Label(box, image=badge)
        label.image = badge
        label.grid(row=0, column=0, padx=(0, 12))
    except tk.TclError:
        pass
    ttk.Label(box, text=text, style="Title.TLabel", wraplength="4.4i", justify="left").grid(
        row=0, column=1, sticky="w")
    return box


def center(root) -> None:
    """Place the window in the middle of the screen once its size is known."""
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{x}+{y}")
