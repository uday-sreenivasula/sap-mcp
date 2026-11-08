"""ABAP source code out of SAP, through ZCFG_READ_SRV Compute ``GET_SOURCE``.

Give it a program, include, class, interface or function module name (or a
transaction code, which stands for the program it starts). SAP resolves the
name, checks S_DEVELOP display authorization for the caller, and returns the
active source. The code is saved under ``Downloads/SAP source``; small
sources also come back inline, large ones as a preview plus the file path.
"""
from __future__ import annotations

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
