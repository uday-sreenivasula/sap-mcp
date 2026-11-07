"""Usage controls: keep SAP data from draining the conversation (and the plan).

Three mechanisms, all applied centrally in server._safe() so every tool gets
them:

1. Large results go to a file, not into the chat. Above SAP_MCP_INLINE_CHARS
   (default 12,000 characters, ~3k tokens) the rows are written to Excel in
   Downloads\\SAP exports; the answer carries the row count, columns, a short
   preview, totals of amount columns and the file path.
2. A per-conversation allowance: SAP_MCP_SESSION_BUDGET_CHARS characters and
   SAP_MCP_MAX_CALLS calls. Going past either opens a window on the user's
   screen; only a click there continues. The model cannot extend it itself.
3. A usage log, ~/.sap-mcp/usage_log.jsonl: one line per call (tool, size,
   duration, saved to file or not, error).
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from . import credentials


def _env_int(name: str, default: int, floor: int = 1) -> int:
    try:
        return max(floor, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


#: Answers larger than this are saved to a file instead of returned.
INLINE_CHARS = _env_int("SAP_MCP_INLINE_CHARS", 12_000, floor=2_000)
#: SAP calls per conversation before the user is asked.
MAX_CALLS = _env_int("SAP_MCP_MAX_CALLS", 80)
PREVIEW_ROWS = 10
_DECIMAL = re.compile(r"^-?\d+\.\d+$")


# ------------------------------------------------------------- spill to file

def export_folder() -> Path:
    override = os.environ.get("SAP_MCP_EXPORT_DIR")
    return Path(override) if override else Path.home() / "Downloads" / "SAP exports"


def _rows_of(result: Any) -> Optional[tuple[str, list, Optional[list]]]:
    """(key, rows, columns) of the main table in a tool result, if any."""
    if isinstance(result, list) and result:
        return "", result, None
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("rows"), list) and result["rows"]:
        return "rows", result["rows"], result.get("columns")
    d = result.get("d")
    if isinstance(d, dict) and isinstance(d.get("results"), list) and d["results"]:
        return "d", d["results"], None
    lists = [(k, v) for k, v in result.items() if isinstance(v, list) and v]
    if not lists:
        return None
    key, rows = max(lists, key=lambda kv: len(json.dumps(kv[1], default=str)))
    return key, rows, None


def _as_dicts(rows: list, columns: Optional[list]) -> list[dict]:
    if columns and rows and isinstance(rows[0], list):
        return [dict(zip(columns, r)) for r in rows]
    return [r if isinstance(r, dict) else {"value": r} for r in rows]


def _cell(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return v


def _totals(rows: list[dict], cols: list[str]) -> dict:
    """Sums of columns that hold amounts (decimal numbers), exact."""
    out = {}
    for c in cols:
        total, decimal_seen, ok = Decimal(0), False, False
        for r in rows:
            v = r.get(c)
            if v in (None, ""):
                continue
            if isinstance(v, bool):
                ok = False
                break
            if isinstance(v, float) or (isinstance(v, str) and _DECIMAL.match(v.strip())):
                decimal_seen = True
            elif not isinstance(v, int):
                ok = False
                break
            try:
                total += Decimal(str(v).strip())
                ok = True
            except InvalidOperation:
                ok = False
                break
        if ok and decimal_seen:
            out[c] = format(total, "f")
    return out


def _write(path: Path, cols: list[str], rows: list[dict]) -> Path:
    try:
        from .reports import write_xlsx

        write_xlsx(path, [{"field": c, "text": c} for c in cols], rows)
        return path
    except ImportError:  # openpyxl missing: CSV still opens in Excel
        import csv

        path = path.with_suffix(".csv")
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([_cell(r.get(c)) for c in cols])
        return path


def _preview(rows: list[dict], cols: list[str]) -> list[list]:
    """First rows, cells shortened, and fewer rows if they are very wide."""
    def short(v):
        return v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v
    preview = [[short(r.get(c)) for c in cols] for r in rows[:PREVIEW_ROWS]]
    while len(preview) > 1 and len(json.dumps(preview, default=str)) > INLINE_CHARS // 2:
        preview = preview[: len(preview) // 2]
    return preview


def spill(result: Any, tool: str, size: int) -> Optional[dict]:
    """Save a large result's rows to Excel; return a small answer about it."""
    found = _rows_of(result)
    if not found:
        return None
    key, raw_rows, columns = found
    rows = [{k: _cell(v) for k, v in r.items()} for r in _as_dicts(raw_rows, columns)]
    cols: list[str] = list(columns) if columns else []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)

    folder = export_folder()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _write(folder / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', tool)}_{stamp}.xlsx", cols, rows)

    out: dict[str, Any] = {}
    if isinstance(result, dict):  # keep the small, descriptive fields
        for k, v in result.items():
            if k in (key, "columns") or isinstance(v, list):
                continue
            if len(json.dumps(v, default=str)) <= 1_500:
                out[k] = v
    out.update({
        "saved_to": str(path),
        "row_count": len(rows),
        "columns": cols,
        "preview": _preview(rows, cols),
        "amount_totals": _totals(rows, cols),
        "note": (f"The full result ({len(rows):,} rows, ~{size // 4:,} tokens) was saved to the file "
                 "instead of being sent into the conversation. Use the preview and totals, read "
                 "the file if needed, or ask SAP for totals (aggregate_sap_table) or fewer rows."),
    })
    return out


# ------------------------------------------------------ the user's permission

def ask_to_continue(state: dict) -> bool:
    """Window on the user's screen: allow this conversation more SAP data?"""
    request = {
        "title": "We are pulling a lot of data from SAP. Is something wrong?",
        "lines": [
            f"This conversation has already pulled ~{state['approx_tokens_spent']:,} tokens of SAP "
            f"data in {state['calls']} calls (limit ~{state['budget_chars'] // 4:,} tokens / "
            f"{state['max_calls']} calls).",
            "Please check what you are asking for. A narrower question, or asking for totals "
            "instead of rows, usually needs far less.",
            "If this is absolutely required, press Continue to reset the limit and pull more. "
            "This will use your Claude usage limits.",
        ],
        "yes": "Continue",
        "no": "Stop",
    }
    child_env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "sap_mcp.confirm_dialog"],
            input=json.dumps(request), capture_output=True, text=True, timeout=6 * 60,
            env=child_env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return bool(json.loads(proc.stdout.strip().splitlines()[-1]).get("approved"))
    except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
        return False


# -------------------------------------------------------------- usage log

def log_call(entry: dict) -> None:
    entry = {"at": _dt.datetime.now().isoformat(timespec="seconds"), **entry}
    try:
        path = credentials.config_dir() / "usage_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        pass
