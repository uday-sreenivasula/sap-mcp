"""Run SAP GUI reports (SE38 / transaction reports) and export their output.

Through ZCFG_READ_SRV Compute:

* ``LIST_REPORTS``: executable reports matching a pattern, with titles.
* ``REPORT_INFO``: a report's selection fields and saved variants.
* ``RUN_REPORT``: SUBMIT ... AND RETURN with a variant and/or selections.
  ALV output comes back as rows with column headings (CL_SALV_BS_RUNTIME_INFO);
  classic list output as text lines.

The full result is saved as an Excel workbook on this PC; the answer only
carries the file path, the columns and a short preview.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from typing import Any

from .compute import compute
from .config_tables import MAX_ROWS
from .sap_client import SCAN_SECONDS, SapODataClient

#: A report runs inside one HTTP call: give it the scan budget, not the
#: 30-second per-request limit.
REPORT_SECONDS = SCAN_SECONDS
PREVIEW_ROWS = 15
_PROGRAM = re.compile(r"^[A-Z0-9_/]{1,40}$")
_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _program(report: str) -> str:
    name = (report or "").strip().upper()
    if not _PROGRAM.match(name):
        raise ValueError(f"{report!r} is not a valid report name.")
    return name


def default_folder() -> Path:
    override = os.environ.get("SAP_MCP_REPORTS_DIR")
    return Path(override) if override else Path.home() / "Downloads" / "SAP reports"


def list_reports(client: SapODataClient, pattern: str = "Z*", top: int = 200) -> dict:
    pat = (pattern or "Z*").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_/*%]+", pat):
        raise ValueError("pattern may contain letters, digits, _, / and * only, e.g. 'ZFI*'.")
    rows = compute(client, "LIST_REPORTS", {"REPORT": pat, "MAX_ROWS": max(1, min(int(top), MAX_ROWS))})
    progs = [{"report": r.get("NAME"), "title": r.get("TEXT", "")} for r in rows if r.get("KIND") != "TCODE"]
    tcodes = [{"tcode": r.get("NAME"), "program": r.get("TYPE", ""), "title": r.get("TEXT", "")}
              for r in rows if r.get("KIND") == "TCODE"]
    return {"pattern": pat, "reports": progs, "transactions": tcodes,
            "hint": "run_sap_report and sap_report_info accept a program name or a transaction code."}


def report_info(client: SapODataClient, report: str) -> dict:
    name = _program(report)
    rows = compute(client, "REPORT_INFO", {"REPORT": name})
    sels = [{"name": r["NAME"], "kind": r.get("KIND"), "type": r.get("TYPE", ""), "text": r.get("TEXT", "")}
            for r in rows if r.get("KIND") in ("PARAMETER", "SELECT-OPTION")]
    variants = [{"variant": r["NAME"], "text": r.get("TEXT", "")} for r in rows if r.get("KIND") == "VARIANT"]
    return {"report": name, "selections": sels, "variants": variants,
            "hint": "Run with run_sap_report(report, variant=..., selections={...}). "
                    "A saved variant is the easiest way to reproduce what users run in SAP GUI."}


def _selections(selections: Any) -> list[dict]:
    """Friendly selections -> RSPARAMS rows (SELNAME, SIGN, OPTION, LOW, HIGH).

    Accepts {"S_BUKRS": "1000", "S_LIFNR": ["100045", "100046"],
    "S_BUDAT": {"low": "20260401", "high": "20260930"}, "P_KEYDT": "20260930"}
    or a list of RSPARAMS-like dicts.
    """
    if not selections:
        return []
    out: list[dict] = []
    if isinstance(selections, list):
        for s in selections:
            out.append({k.upper(): str(v) for k, v in s.items() if v not in (None, "")})
        return out
    for name, value in dict(selections).items():
        values = value if isinstance(value, list) else [value]
        for v in values:
            row = {"SELNAME": name.upper()}
            if isinstance(v, dict):
                row.update({k.upper(): str(x) for k, x in v.items() if x not in (None, "")})
            else:
                row["LOW"] = str(v)
            out.append(row)
    return out


def write_xlsx(path: Path, columns: list[dict], rows: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Report"
    ws.append([c["text"] or c["field"] for c in columns])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in rows:
        ws.append([_cell(r.get(c["field"])) for c in columns])
    ws.freeze_panes = "A2"
    for i, c in enumerate(columns, start=1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = min(40, max(10, len(c["text"] or c["field"]) + 2))
    wb.save(path)


def _cell(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+\.\d+", s):  # amounts arrive as exact strings
            try:
                return float(s)
            except ValueError:
                return s
        return s
    if isinstance(v, (dict, list)):
        return str(v)
    return v


def run_report(
    client: SapODataClient,
    report: str,
    *,
    variant: str = "",
    selections: Any = None,
    max_rows: int = 5000,
    folder: str = "",
) -> dict:
    name = _program(report)
    rows = compute(client, "RUN_REPORT", {
        "REPORT": name, "VARIANT": variant.strip().upper(),
        "SELECTIONS": _selections(selections), "MAX_ROWS": max(1, min(int(max_rows), 5000)),
    }, deadline=REPORT_SECONDS)

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    where = Path(folder) if folder else default_folder()
    where.mkdir(parents=True, exist_ok=True)
    base = _SAFE.sub("_", f"{name}{'_' + variant.upper() if variant else ''}_{stamp}")

    if rows and "COLUMNS" in rows[0]:  # ALV: row 0 holds the headings
        columns = [{"field": c["FIELD"], "text": c.get("TEXT", "")} for c in rows[0]["COLUMNS"]
                   if not c.get("HIDDEN")]
        data = rows[1:]
        known = {c["field"] for c in columns}
        if data and not known & set(data[0]):
            columns = [{"field": k, "text": k} for k in data[0]]
        target = where / f"{base}.xlsx"
        write_xlsx(target, columns, data)
        preview = [[_cell(r.get(c["field"])) for c in columns] for r in data[:PREVIEW_ROWS]]
        return {
            "report": name, "variant": variant.upper() or None, "output": "ALV",
            "rows": len(data), "saved_to": str(target),
            "columns": [c["text"] or c["field"] for c in columns],
            "preview": preview,
            "note": (f"Full output in the Excel file; {len(data)} rows"
                     + (" (row limit reached, narrow the selection)" if len(data) >= max_rows else "") + "."),
        }

    lines = [r.get("LINE", "") for r in rows]
    target = where / f"{base}.txt"
    target.write_text("\n".join(lines), encoding="utf-8")
    return {
        "report": name, "variant": variant.upper() or None, "output": "list",
        "lines": len(lines), "saved_to": str(target),
        "preview": lines[:40],
        "note": "Classic list output, saved as text. Ask to read the file for more.",
    }
