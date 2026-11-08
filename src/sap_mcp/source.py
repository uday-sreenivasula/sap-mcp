"""ABAP source code out of SAP, through ZCFG_READ_SRV Compute ``GET_SOURCE``.

Give it a program, include, class, interface or function module name (or a
transaction code, which stands for the program it starts). SAP resolves the
name, checks S_DEVELOP display authorization for the caller, and returns the
active source. The code is saved under ``Downloads/SAP source``; small
sources also come back inline, large ones as a preview plus the file path.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .compute import compute
from .sap_client import SCAN_SECONDS, SapODataClient, SapODataError

SOURCE_SECONDS = SCAN_SECONDS
#: Inline the whole source only up to this size; it stays under the usage
#: controls' inline limit so the answer is never spilled to Excel.
INLINE_CHARS = int(os.environ.get("SAP_MCP_SOURCE_INLINE_CHARS", "10000"))
PREVIEW_LINES = 60
OBJECT_TYPES = ("PROG", "INCL", "CLAS", "INTF", "FUNC", "TCODE")
_NAME = re.compile(r"^[A-Z0-9_/<>=-]{1,40}$")
_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _object_name(name: str) -> str:
    clean = (name or "").strip().upper()
    if not _NAME.match(clean):
        raise ValueError(f"{name!r} is not a valid ABAP object name.")
    return clean


def default_folder() -> Path:
    override = os.environ.get("SAP_MCP_SOURCE_DIR")
    return Path(override) if override else Path.home() / "Downloads" / "SAP source"


def get_source(
    client: SapODataClient,
    name: str,
    *,
    object_type: str = "",
    with_includes: bool = False,
    folder: str = "",
) -> dict:
    obj = _object_name(name)
    typ = (object_type or "").strip().upper()
    if typ and typ not in OBJECT_TYPES:
        raise ValueError(f"object_type must be one of {', '.join(OBJECT_TYPES)} (or empty).")
    try:
        rows = compute(client, "GET_SOURCE", {
            "REPORT": obj, "OBJECT_TYPE": typ, "WITH_INCLUDES": bool(with_includes),
        }, deadline=SOURCE_SECONDS)
    except SapODataError as e:
        if "Unknown Operation" in str(e):
            raise SapODataError(
                e.status_code,
                "The ZCFG_READ_SRV service on this SAP system is an older version without "
                "GET_SOURCE. The updated ZCL_ZCFG_READ_DPC_EXT has to be activated in the "
                "development system and transported (sap_side/README.md, section 7).",
                e.body,
            ) from e
        raise

    units = [r for r in rows if r.get("KIND") == "SOURCE"]
    listed = [r for r in rows if r.get("KIND") == "INCLUDE"]
    if not units:
        raise SapODataError(404, f"SAP returned no source for {obj}.", "")

    main = units[0]
    where = Path(folder) if folder else default_folder()
    if len(units) > 1:
        where = where / _SAFE.sub("_", main["NAME"])
    where.mkdir(parents=True, exist_ok=True)

    objects = []
    for u in units:
        target = where / f"{_SAFE.sub('_', u['NAME'])}.abap"
        target.write_text(u.get("SOURCE", ""), encoding="utf-8")
        entry = {"name": u["NAME"], "type": u.get("TYPE"), "title": u.get("TITLE") or None,
                 "lines": int(u.get("LINE_COUNT") or 0), "saved_to": str(target)}
        if u.get("TRUNCATED"):
            entry["truncated"] = True
        objects.append(entry)

    out: dict = {
        "object": main["NAME"], "type": main.get("TYPE"), "title": main.get("TITLE") or None,
        "objects": objects,
    }
    if main.get("VIA"):
        out["resolved_via"] = main["VIA"]
    if listed:
        out["includes_not_fetched"] = [{"name": r["NAME"], "title": r.get("TITLE") or None} for r in listed]
        if not with_includes and any(r["NAME"][:1] in "ZY/" for r in listed):
            out["hint"] = "Call again with with_includes=true to fetch the customer includes as well."

    total = sum(len(u.get("SOURCE", "")) for u in units)
    if total <= INLINE_CHARS:
        out["source"] = {u["NAME"]: u.get("SOURCE", "") for u in units}
    else:
        lines = main.get("SOURCE", "").splitlines()
        out["preview"] = "\n".join(lines[:PREVIEW_LINES])
        out["note"] = (f"{total:,} characters of source were saved to the file(s) above; only the first "
                       f"{PREVIEW_LINES} lines of {main['NAME']} are shown here. Read the saved file for the rest.")
    return out


# ------------------------------------------------ text pool, GUI status, screens

TEXT_IDS = {"R": "title", "I": "text_elements", "S": "selection_texts", "H": "list_headers"}


def _clean(rows: list[dict]) -> list[dict]:
    """Lower-case keys, blanks dropped: CUA tables are wide and mostly empty."""
    return [{k.lower(): v for k, v in r.items() if v not in (None, "", " ", 0, False)} for r in rows]


def program_parts(client: SapODataClient, name: str, *, folder: str = "") -> dict:
    obj = _object_name(name)
    try:
        rows = compute(client, "PROGRAM_PARTS", {"REPORT": obj}, deadline=SOURCE_SECONDS)
    except SapODataError as e:
        if "Unknown Operation" in str(e):
            raise SapODataError(
                e.status_code,
                "The ZCFG_READ_SRV service on this SAP system is an older version without "
                "PROGRAM_PARTS (sap_side/README.md, section 7).", e.body) from e
        raise

    texts: dict = {}
    for r in rows:
        if r.get("KIND") != "TEXT":
            continue
        bucket = TEXT_IDS.get(r.get("ID"), r.get("ID"))
        entry = r.get("TEXT", "") or ""
        if bucket == "title":
            texts["title"] = entry
        elif bucket == "selection_texts":
            # Text pool 'S' entries: 8-char flag area ('D' = text taken from the
            # data element), then the text.
            text = entry[8:].strip() if len(entry) > 8 else ""
            texts.setdefault(bucket, {})[r.get("KEY", "")] = text or (
                "(from data element)" if entry[:1] == "D" else "")
        else:
            texts.setdefault(bucket, {})[r.get("KEY", "")] = entry

    cua = next((r for r in rows if r.get("KIND") == "CUA"), None)
    gui = None
    if cua:
        gui = {
            "statuses": _clean(cua.get("STA") or []),
            "function_texts": _clean(cua.get("FUN") or []),
            "titles": _clean(cua.get("TIT") or []),
            "function_keys": _clean(cua.get("PFK") or []),
            "pushbuttons": _clean(cua.get("BUT") or []),
            "menu_bars": _clean(cua.get("ACT") or []),
            "menus": _clean(cua.get("MEN") or []),
            "menu_texts": _clean(cua.get("MTX") or []),
            "status_sets": _clean(cua.get("SET") or []),
        }
    screens = [{"screen": r.get("DYNNR"), "type": r.get("TYPE"),
                "title": ("selection screen" if str(r.get("TITLE") or "").startswith("SEL_SCREEN")
                          else r.get("TITLE") or None)}
               for r in rows if r.get("KIND") == "SCREEN"]

    full = {"program": obj, "texts": texts, "gui": gui, "screens": screens}
    where = Path(folder) if folder else default_folder()
    where.mkdir(parents=True, exist_ok=True)
    target = where / f"{_SAFE.sub('_', obj)}.parts.json"
    target.write_text(json.dumps(full, indent=1, ensure_ascii=False), encoding="utf-8")

    out: dict = {"program": obj, "saved_to": str(target), "texts": texts, "screens": screens}
    if gui:
        out["gui"] = {"statuses": gui["statuses"], "titles": gui["titles"],
                      "function_texts": gui["function_texts"]}
        out["gui_note"] = ("Function keys, pushbuttons and menus per status are in the saved file "
                           "(keys function_keys, pushbuttons, menu_bars, menus, menu_texts).")
    else:
        out["gui"] = None
    if len(json.dumps(out, ensure_ascii=False)) > INLINE_CHARS:
        out["gui"] = {"statuses": gui["statuses"] if gui else [], "titles": gui["titles"] if gui else []}
        out["texts"] = {k: (v if k == "title" else f"{len(v)} entries, see file") for k, v in texts.items()}
        out["note"] = "Large: details are in the saved file."
    return out
