"""Calculations done inside SAP via ZCFG_READ_SRV's single ``Compute`` function.

``Compute(Operation, Params)``: Params is a JSON object with UPPER-CASE keys,
and each result row comes back as JSON in ``TableRows.Data``. One function
import covers every operation, so new calculations need no SEGW change on the
SAP side, only ABAP code in ZCL_ZCFG_READ_DPC_EXT.

Amounts are parsed as Decimal and returned as strings, so large totals
are exact (no float rounding).
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Optional

from .config_tables import (
    MAX_ROWS, _call, _field_names, _odata_string, _results, _table_name, tabular,
)
from .sap_client import SapODataClient

_MEASURE_FUNCS = ("SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD")
_DATE = re.compile(r"^\d{8}$")
#: Accounts returned inline by trial_balance; the rest are in the Excel file.
TB_INLINE_ROWS = 150


def compute(
    client: SapODataClient, operation: str, params: dict, deadline: Optional[float] = None,
) -> list[dict]:
    """Run one Compute operation; returns the result rows as dicts."""
    clean = {k.upper(): v for k, v in params.items() if v not in (None, "")}
    payload = _call(client, "Compute", {
        "Operation": _odata_string(operation),
        "Params": _odata_string(json.dumps(clean, separators=(",", ":"))),
    }, deadline=deadline)
    rows = []
    for r in sorted(_results(payload), key=lambda r: int(r.get("RowNo") or 0)):
        row = json.loads(r.get("Data") or "{}", parse_float=Decimal)
        rows.append({k.upper(): (_dec(v) if isinstance(v, Decimal) else v) for k, v in row.items()})
    return rows


def _dec(v: Decimal) -> str:
    """Decimal as a plain, exact string (no exponent)."""
    return format(v, "f")


def _date(value: str, name: str) -> str:
    """Accept YYYY-MM-DD or YYYYMMDD; SAP wants YYYYMMDD."""
    v = (value or "").replace("-", "").strip()
    if v and not _DATE.match(v):
        raise ValueError(f"{name} must be a date like 2026-03-31.")
    return v


def _measures(measures: str) -> list[str]:
    out = []
    for m in re.split(r"[\s,]+", measures or "COUNT"):
        if not m:
            continue
        func, _, field = m.strip().upper().partition(":")
        if func not in _MEASURE_FUNCS:
            raise ValueError(f"Unknown measure {m!r}: use SUM:f, AVG:f, MIN:f, MAX:f, COUNT or COUNTD:f.")
        if field:
            _field_names(field)
        out.append(f"{func}:{field}" if field else func)
    return out


def _zeros(rows: list[dict], cols: list[str]) -> list[dict]:
    """SAP leaves zero amounts out of the JSON; show them as 0."""
    return [{**r, **{c: r.get(c) if r.get(c) not in (None, "") else "0" for c in cols}} for r in rows]


def _limit(top: int) -> int:
    return max(1, min(int(top), MAX_ROWS))


def _with_more(out: dict, rows: list, top: int, hint: str) -> dict:
    if len(rows) > top:
        out["more_available"] = True
        out["hint"] = hint
    return out


# ------------------------------------------------------------- operations

def aggregate(
    client: SapODataClient, table: str, *, measures: str = "COUNT", group_by: str = "",
    where: str = "", order_by: str = "", top: int = 500,
) -> dict:
    table = _table_name(table)
    groups = _field_names(group_by)
    meas = _measures(measures)
    top = _limit(top)
    rows = compute(client, "AGGREGATE", {
        "TABLE_NAME": table, "WHERE_COND": where, "GROUP_BY": ",".join(groups),
        "MEASURES": ",".join(meas), "ORDER_BY": order_by.strip(), "MAX_ROWS": top + 1,
    })
    out: dict[str, Any] = {"table": table, "group_by": groups, "measures": meas}
    if where:
        out["where"] = where
    if groups:
        cols = groups + [k for k in (rows[0] if rows else {}) if k not in groups]
        out["groups"] = min(len(rows), top)
        out.update(tabular(rows[:top], cols))
    else:
        out["totals"] = rows[0] if rows else {}
    return _with_more(out, rows, top, f"More than {top} groups: tighten where, group by fewer fields, or raise top.")


def duplicates(
    client: SapODataClient, table: str, fields: str, *, where: str = "", min_count: int = 2, top: int = 200,
) -> dict:
    table = _table_name(table)
    keys = _field_names(fields)
    if not keys:
        raise ValueError("fields is required, e.g. 'LIFNR,XBLNR,RMWWR'.")
    top = _limit(top)
    rows = compute(client, "DUPLICATES", {
        "TABLE_NAME": table, "WHERE_COND": where, "FIELDS": ",".join(keys),
        "MIN_COUNT": max(2, int(min_count)), "MAX_ROWS": top + 1,
    })
    out: dict[str, Any] = {"table": table, "fields": keys, "duplicate_groups": min(len(rows), top)}
    if where:
        out["where"] = where
    out.update(tabular(rows[:top], keys + ["ROW_COUNT"]))
    if not rows:
        out["note"] = "No duplicates: every combination of these fields occurs once."
    return _with_more(out, rows, top, "More duplicate groups than shown: tighten where.")


def trial_balance(
    client: SapODataClient, company_code: str, fiscal_year: str, *, period_from: int = 1,
    period_to: int = 16, account_from: str = "", account_to: str = "", ledger: str = "0L",
) -> dict:
    rows = compute(client, "TRIAL_BALANCE", {
        "COMPANY_CODE": company_code.strip().upper(), "FISCAL_YEAR": str(fiscal_year).strip(),
        "PERIOD_FROM": int(period_from), "PERIOD_TO": int(period_to),
        "ACCOUNT_FROM": account_from.strip(), "ACCOUNT_TO": account_to.strip(),
        "LEDGER": ledger.strip().upper(),
    })
    cols = ["RACCT", "TXT50", "OPENING", "DEBIT", "CREDIT", "CLOSING"]
    rows = _zeros(rows, cols[2:])
    totals = {c: _dec(sum((Decimal(str(r.get(c) or 0)) for r in rows), Decimal(0)))
              for c in ("OPENING", "DEBIT", "CREDIT", "CLOSING")}
    periods = f"{int(period_from):03d}-{int(period_to):03d}"
    out: dict[str, Any] = {
        "company_code": company_code.upper(), "fiscal_year": str(fiscal_year), "ledger": ledger.upper(),
        "periods": periods, "accounts": len(rows), "totals": totals,
        "source": "ACDOCA, computed in SAP (Compute TRIAL_BALANCE)",
    }
    # A full company trial balance is several hundred accounts: always save it
    # to Excel; put every row in the answer only when that stays small.
    from .reports import default_folder, write_xlsx
    folder = default_folder()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"TB_{company_code.upper()}_{fiscal_year}_P{periods}.xlsx"
    write_xlsx(target, [{"field": c, "text": t} for c, t in zip(
        cols, ["G/L account", "Account name", "Opening", "Debit", "Credit", "Closing"])], rows)
    out["saved_to"] = str(target)
    if len(rows) <= TB_INLINE_ROWS:
        out.update(tabular(rows, cols))
    else:
        out.update(tabular(rows[:TB_INLINE_ROWS], cols))
        out["note"] = (f"Showing the first {TB_INLINE_ROWS} of {len(rows)} accounts; the full trial balance "
                       "is in the Excel file. Use account_from / account_to for a section.")
    return out


def ageing(
    client: SapODataClient, account_type: str, company_code: str, *, key_date: str = "",
    partner: str = "", ledger: str = "0L", top: int = 500,
) -> dict:
    kind = {"vendor": "K", "vendors": "K", "k": "K", "customer": "D", "customers": "D", "d": "D"}.get(
        account_type.strip().lower())
    if not kind:
        raise ValueError("account_type must be 'vendor' or 'customer'.")
    top = _limit(top)
    rows = compute(client, "AGEING", {
        "ACCOUNT_TYPE": kind, "COMPANY_CODE": company_code.strip().upper(),
        "KEY_DATE": _date(key_date, "key_date"), "PARTNER": partner.strip(),
        "LEDGER": ledger.strip().upper(), "MAX_ROWS": top + 1,
    })
    cols = ["PARTNER", "NAME1", "TOTAL", "NOT_DUE", "D001_030", "D031_060", "D061_090", "D091_180", "OVER_180"]
    rows = _zeros(rows, cols[2:])
    totals = {c: _dec(sum((Decimal(str(r.get(c) or 0)) for r in rows[:top]), Decimal(0))) for c in cols[2:]}
    out = {
        "account_type": "vendor" if kind == "K" else "customer",
        "company_code": company_code.upper(), "key_date": key_date or "today",
        "partners": min(len(rows), top), "totals": totals, **tabular(rows[:top], cols),
        "note": "Buckets are days past the net due date at the key date; amounts signed in company-code currency "
                "(vendor credits negative).",
    }
    return _with_more(out, rows, top, "More partners than shown: filter by partner.")


def grir(
    client: SapODataClient, company_code: str, *, vendor: str = "", po: str = "",
    key_date: str = "", top: int = 500,
) -> dict:
    top = _limit(top)
    rows = compute(client, "GRIR", {
        "COMPANY_CODE": company_code.strip().upper(), "VENDOR": vendor.strip(), "PO": po.strip(),
        "KEY_DATE": _date(key_date, "key_date"), "MAX_ROWS": top + 1,
    })
    cols = ["EBELN", "EBELP", "LIFNR", "GR_QTY", "IR_QTY", "GR_VALUE", "IR_VALUE", "DIFF_VALUE"]
    rows = _zeros(rows, cols[3:])
    total = _dec(sum((Decimal(str(r.get("DIFF_VALUE") or 0)) for r in rows[:top]), Decimal(0)))
    out = {
        "company_code": company_code.upper(), "key_date": key_date or "today",
        "items_with_difference": min(len(rows), top), "total_difference": total,
        **tabular(rows[:top], cols),
        "note": "GR minus IR value per PO item (EKBE, company-code currency). Positive = received, "
                "not yet invoiced.",
    }
    return _with_more(out, rows, top, "More items than shown: filter by vendor or po.")
