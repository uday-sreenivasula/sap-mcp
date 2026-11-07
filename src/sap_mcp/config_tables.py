"""Read SAP configuration (customizing) tables.

Standard OData services expose business documents, not customizing, so this
module talks to one small custom Gateway service, ``ZCFG_READ_SRV`` (ABAP
source and setup steps in ``sap_side/``). It offers two GET function imports:

* ``ReadTable(TableName, Where, Fields, MaxRows, Skip)`` — rows of any table
  the user may display under S_TABU_NAM, each row returned as a JSON string.
* ``GetTableFields(TableName)`` — the DDIC field list with descriptions.

``Where`` is an **ABAP Open SQL** condition (``KAPPL = 'M' AND KSCHL LIKE
'Z%'``), not an OData $filter. The service runs it inside SAP, under the
caller's own authorizations, read-only.

On top of the generic reader sit a few typed shortcuts for common questions
(condition types, pricing procedures, MM account determination) so callers do
not need to know table names. The shortcuts only ask for fields that exist on
this system: they intersect their wish list with the table's real field list
(cached), so a field missing on one release never breaks them.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterable, Optional

from . import environments as envs
from .sap_client import METADATA_TTL_DAYS, SapODataClient, SapODataError

SERVICE = "ZCFG_READ_SRV"

#: Hard ceiling on rows per call. The ABAP side caps at 5000; the MCP stays
#: well below so a single answer can never swamp the context window.
MAX_ROWS = 1000

#: Chart(s) of accounts account_determination uses unless told otherwise:
#: SAP_MCP_CHARTS, else "charts" in the site settings, else all. Most systems
#: also carry ~30 SAP sample charts (CABE, CACZ, CARU, ...) that flood every
#: answer, so set your own chart(s). Callers pass "*" for all.
_CHARTS_RAW = os.environ.get("SAP_MCP_CHARTS") or str(envs.site_value("charts", "") or "")
DEFAULT_CHARTS = [
    c.strip().upper() for c in _CHARTS_RAW.split(",") if c.strip() and c.strip() != "*"
]

#: Rows per page for the condition-type list.
CONDITION_PAGE = 300

_NAME_RE = re.compile(r"^[A-Z0-9_/]{1,30}$")

#: Field-list cache: str(cache file path) -> list of field dicts.
_fields_mem: dict[str, list[dict]] = {}


def abap_literal(value: Any) -> str:
    """Quote a value as an ABAP SQL literal (single quotes doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def in_list(field: str, values: Iterable[Any]) -> str:
    return f"{field} IN ({', '.join(abap_literal(v) for v in values)})"


def _odata_string(value: str) -> str:
    """OData v2 function-import string parameter: quoted, quotes doubled."""
    return "'" + value.replace("'", "''") + "'"


def _table_name(table: str) -> str:
    name = (table or "").strip().upper()
    if not _NAME_RE.match(name):
        raise ValueError(f"{table!r} is not a valid SAP table name.")
    return name


def _field_names(fields: Any) -> list[str]:
    if not fields:
        return []
    if isinstance(fields, str):
        fields = re.split(r"[\s,]+", fields)
    out = []
    for f in fields:
        f = str(f).strip().upper()
        if f and f not in out:
            if not _NAME_RE.match(f):
                raise ValueError(f"{f!r} is not a valid field name.")
            out.append(f)
    return out


def _results(payload: dict) -> list[dict]:
    """Rows from a function-import response, whichever v2 JSON shape it uses."""
    d = payload.get("d", payload) if isinstance(payload, dict) else payload
    if isinstance(d, dict):
        d = d.get("results", d)
    return d if isinstance(d, list) else []


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        v = value.strip()
        return v in ("", "0000-00-00", "00000000", "000000")
    return False


def compact(row: dict) -> dict:
    """Drop blank fields — most customizing columns are empty flags."""
    return {k: v for k, v in row.items() if not _is_empty(v)}


def _call(client: SapODataClient, function: str, params: dict, deadline: Optional[float] = None) -> dict:
    try:
        return client.get(f"{SERVICE}/{function}", params=params, deadline=deadline)
    except SapODataError as e:
        text = f"{e} {e.body}"
        if "Resource not found for the segment" in text:
            # The service is there, but an older version without this function.
            raise SapODataError(
                e.status_code,
                f"{SERVICE} on this SAP system ({client.config.base_url}, client "
                f"{client.config.client}) is an older version without '{function}'. "
                "The updated service has to be activated in the development system and transported "
                "(see sap_side/README.md).",
                e.body,
            ) from e
        if e.status_code == 404 or "No service found" in text or (
            e.status_code == 403 and SERVICE in text
        ):
            raise SapODataError(
                e.status_code,
                f"The SAP table service {SERVICE} is not available on this SAP "
                f"system ({client.config.base_url}, client {client.config.client}). "
                "It has to be built in the development system, transported and activated in "
                "/IWFND/MAINT_SERVICE — see sap_side/README.md in the sap-mcp folder.",
                e.body,
            ) from e
        raise


# ----------------------------------------------------------------- generic

def read_table(
    client: SapODataClient,
    table: str,
    *,
    where: str = "",
    fields: Any = None,
    max_rows: int = 100,
    skip: int = 0,
    drop_empty: bool = True,
) -> dict:
    """Read rows of one table. Key fields are always included by SAP."""
    table = _table_name(table)
    wanted = _field_names(fields)
    max_rows = max(1, min(int(max_rows), MAX_ROWS))
    skip = max(0, int(skip))
    payload = _call(client, "ReadTable", {
        "TableName": _odata_string(table),
        "Where": _odata_string(where or ""),
        "Fields": _odata_string(",".join(wanted)),
        # One extra row tells us whether there is more without a count query.
        "MaxRows": max_rows + 1,
        "Skip": skip,
    })
    raw = sorted(_results(payload), key=lambda r: int(r.get("RowNo") or 0))
    rows = []
    for r in raw[:max_rows]:
        try:
            row = json.loads(r.get("Data") or "{}")
        except ValueError:
            row = {"_unparsed": r.get("Data")}
        rows.append(compact(row) if drop_empty else row)
    out: dict[str, Any] = {"table": table, "returned": len(rows), "rows": rows}
    if where:
        out["where"] = where
    if len(raw) > max_rows:
        out["more_available"] = True
        out["next_skip"] = skip + max_rows
    return out


def table_fields(client: SapODataClient, table: str, refresh: bool = False) -> list[dict]:
    """DDIC field list for ``table``, cached in memory and on disk."""
    table = _table_name(table)
    path = client.cache_dir("tables") / f"{table.replace('/', '_')}.json"
    key = str(path)
    ttl = METADATA_TTL_DAYS * 86_400
    if not refresh:
        if key in _fields_mem:
            return _fields_mem[key]
        if ttl > 0:
            try:
                if time.time() - path.stat().st_mtime < ttl:
                    _fields_mem[key] = json.loads(path.read_text(encoding="utf-8"))
                    return _fields_mem[key]
            except (OSError, ValueError):
                pass

    payload = _call(client, "GetTableFields", {"TableName": _odata_string(table)})
    fields = []
    for r in sorted(_results(payload), key=lambda r: int(r.get("Position") or 0)):
        f: dict[str, Any] = {
            "name": r.get("FieldName", ""),
            "type": r.get("DataType", ""),
            "length": int(r.get("Length") or 0),
            "text": r.get("Description", ""),
        }
        if r.get("KeyFlag") in ("X", True):
            f["key"] = True
        if int(r.get("Decimals") or 0):
            f["decimals"] = int(r["Decimals"])
        if r.get("CheckTable"):
            f["check_table"] = r["CheckTable"]
        fields.append(f)

    _fields_mem[key] = fields
    if ttl > 0 and fields:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fields), encoding="utf-8")
        except OSError:
            pass
    return fields


def describe_table(
    client: SapODataClient, table: str, search: str = "", refresh: bool = False
) -> dict:
    fields = table_fields(client, table, refresh)
    if not fields:
        return {"table": table.upper(), "error": f"Table {table.upper()} has no fields or does not exist."}
    needle = (search or "").strip().lower()
    shown = [
        f for f in fields
        if not needle or needle in f["name"].lower() or needle in f["text"].lower()
    ]
    return {
        "table": table.upper(),
        "field_count": len(fields),
        "keys": [f["name"] for f in fields if f.get("key")],
        "returned": len(shown),
        "fields": shown,
    }


def _existing(client: SapODataClient, table: str, wanted: Iterable[str]) -> list[str]:
    names = {f["name"] for f in table_fields(client, table)}
    return [w for w in wanted if w in names]


def _legend(client: SapODataClient, table: str, names: Iterable[str]) -> dict:
    """Field name -> description, for the fields that appear in an answer."""
    texts = {f["name"]: f["text"] for f in table_fields(client, table)}
    return {n: texts.get(n, "") for n in names}


#: Safety stop for the shortcuts' internal reads (pages of MAX_ROWS).
_MAX_PAGES = 20


def _all_rows(client, table, where, fields, drop_empty=True) -> list[dict]:
    """Every matching row, fetched a page at a time.

    For the shortcuts' own lookups, which then trim or page what they return.
    Customizing tables are small, but some (T685 for sales) pass 1000 rows.
    """
    rows: list[dict] = []
    skip = 0
    for _ in range(_MAX_PAGES):
        page = read_table(
            client, table, where=where, fields=fields,
            max_rows=MAX_ROWS, skip=skip, drop_empty=drop_empty,
        )
        rows.extend(page["rows"])
        if not page.get("more_available"):
            return rows
        skip = page["next_skip"]
    raise SapODataError(
        0, f"{table} has more than {_MAX_PAGES * MAX_ROWS:,} matching rows; narrow the query."
    )


def tabular(rows: list[dict], columns: list[str]) -> dict:
    """Rows as ``{"columns": [...], "rows": [[...], ...]}``.

    Field names are sent once instead of on every row, which roughly halves
    long lists. Columns that are blank on every row are left out.
    """
    cols = [c for c in columns if any(not _is_empty(r.get(c)) for r in rows)]
    return {"columns": cols, "rows": [[r.get(c, "") for c in cols] for r in rows]}


def _strip_zeros(account: Any) -> Any:
    """GL accounts the way SAP screens show them (no leading zeros)."""
    if isinstance(account, str) and account.isdigit():
        return account.lstrip("0") or "0"
    return account


# ------------------------------------------------------------- shortcuts

#: Condition-type control fields worth showing in a list (T685A). Intersected
#: with the live field list, so unknown names are simply skipped.
_T685A_LIST_FIELDS = (
    "KRECH", "KOAID", "KNTYP", "KMANU", "KNEGA", "KZBZG", "KGRPE", "KRUEK",
)
_KEY_NOISE = {"MANDT", "CLIENT", "KVEWE", "KALSM"}


def condition_types(
    client: SapODataClient,
    application: str = "V",
    usage: str = "A",
    condition_type: str = "",
    search: str = "",
    language: str = "E",
    limit: int = CONDITION_PAGE,
    offset: int = 0,
) -> dict:
    app = application.strip().upper()
    use = usage.strip().upper()
    kschl = condition_type.strip().upper()
    limit = max(1, min(int(limit), MAX_ROWS))
    offset = max(0, int(offset))
    base_where = f"KVEWE = {abap_literal(use)} AND KAPPL = {abap_literal(app)}"
    if kschl:
        base_where += f" AND KSCHL = {abap_literal(kschl)}"

    heads = _all_rows(client, "T685", base_where, _existing(client, "T685", ["KSCHL", "KOZGF"]))
    texts = {
        r.get("KSCHL"): r.get("VTEXT", "")
        for r in _all_rows(
            client, "T685T",
            base_where + f" AND SPRAS = {abap_literal(language)}",
            ["KSCHL", "VTEXT"],
        )
    }

    control: dict[str, dict] = {}
    control_fields: list[str] = []
    if use == "A":  # T685A holds control data for pricing conditions only
        where_a = f"KAPPL = {abap_literal(app)}" + (f" AND KSCHL = {abap_literal(kschl)}" if kschl else "")
        if kschl:
            # One condition type: every non-blank control field.
            control_fields = [
                f["name"] for f in table_fields(client, "T685A")
                if f["name"] not in _KEY_NOISE and f["name"] not in ("KAPPL", "KSCHL")
                and f["type"] != "CLNT"
            ]
        else:
            control_fields = _existing(client, "T685A", _T685A_LIST_FIELDS)
        for r in _all_rows(client, "T685A", where_a, control_fields):
            control[r.get("KSCHL", "")] = {
                k: v for k, v in r.items() if k not in ("KAPPL", "KSCHL")
            }

    rows = []
    for h in heads:
        ct = h.get("KSCHL", "")
        row = {"condition_type": ct, "text": texts.get(ct, "")}
        if h.get("KOZGF"):
            row["access_sequence"] = h["KOZGF"]
        row.update(control.get(ct, {}))
        rows.append(row)

    needle = search.strip().lower()
    if needle:
        rows = [r for r in rows if needle in r["condition_type"].lower() or needle in r["text"].lower()]
    rows.sort(key=lambda r: r["condition_type"])
    total = len(rows)
    rows = rows[offset:offset + limit]

    used = sorted({k for r in rows for k in r} - {"condition_type", "text", "access_sequence"})
    out: dict[str, Any] = {"application": app, "usage": use, "total": total, "returned": len(rows)}
    if offset + len(rows) < total:
        out["more_available"] = True
        out["next_offset"] = offset + len(rows)
        out["hint"] = "Use search= to find specific condition types instead of paging through all."
    if kschl:
        out["condition_types"] = rows
    else:
        out["condition_types"] = tabular(
            rows, ["condition_type", "text", "access_sequence", *control_fields]
        )
    if used:
        out["field_legend"] = _legend(client, "T685A", used)
    if not rows:
        out["note"] = (
            f"No condition types found for usage {use}, application {app}"
            + (f", condition type {kschl}" if kschl else "") + "."
        )
    return out


def pricing_procedure(
    client: SapODataClient,
    name: str,
    application: str = "",
    usage: str = "A",
    language: str = "E",
) -> dict:
    kalsm = name.strip().upper()
    app = application.strip().upper()
    use = usage.strip().upper()
    where = f"KVEWE = {abap_literal(use)} AND KALSM = {abap_literal(kalsm)}"
    if app:
        where += f" AND KAPPL = {abap_literal(app)}"

    skip_fields = set(_KEY_NOISE) | ({"KAPPL"} if app else set())
    fields = [
        f["name"] for f in table_fields(client, "T683S")
        if f["name"] not in skip_fields and f["type"] != "CLNT"
    ]
    # SAP always sends the key fields back; drop the ones repeated on every row.
    steps = [
        {k: v for k, v in s.items() if k not in skip_fields}
        for s in _all_rows(client, "T683S", where, fields)
    ]
    if not steps:
        return {
            "procedure": kalsm,
            "error": f"No pricing procedure {kalsm} found for usage {use}"
                     + (f", application {app}" if app else "") + ".",
        }
    steps.sort(key=lambda r: (r.get("KAPPL", ""), int(r.get("STUNR") or 0), int(r.get("ZAEHK") or 0)))

    header_texts = {
        r.get("KAPPL"): r.get("VTEXT", "")
        for r in _all_rows(
            client, "T683U", where + f" AND SPRAS = {abap_literal(language)}", ["KAPPL", "VTEXT"]
        )
    }

    cts = sorted({s["KSCHL"] for s in steps if s.get("KSCHL")})
    ct_texts: dict[tuple, str] = {}
    if cts:
        for r in _all_rows(
            client, "T685T",
            f"SPRAS = {abap_literal(language)} AND KVEWE = {abap_literal(use)} AND " + in_list("KSCHL", cts)
            + (f" AND KAPPL = {abap_literal(app)}" if app else ""),
            ["KAPPL", "KSCHL", "VTEXT"],
        ):
            ct_texts[(r.get("KAPPL"), r.get("KSCHL"))] = r.get("VTEXT", "")
    for s in steps:
        if s.get("KSCHL"):
            text = ct_texts.get((s.get("KAPPL", app), s["KSCHL"]))
            if text and not s.get("VTEXT"):
                s["condition_text"] = text

    used = sorted({k for s in steps for k in s} - {"condition_text"})
    return {
        "procedure": kalsm,
        "description": header_texts.get(app) if app else header_texts,
        "usage": use,
        "application": app or sorted({s.get("KAPPL", "") for s in steps}),
        "step_count": len(steps),
        "steps": steps,
        "field_legend": _legend(client, "T683S", used),
    }


def account_determination(
    client: SapODataClient,
    transaction: str = "WRX",
    chart_of_accounts: str = "",
    valuation_class: str = "",
    account_modifier: str = "",
    valuation_modifier: str = "",
    language: str = "E",
) -> dict:
    ktosl = transaction.strip().upper()
    where = f"KTOSL = {abap_literal(ktosl)}"

    chart = chart_of_accounts.strip().upper()
    if chart == "*":
        charts: list[str] = []
    elif chart:
        charts = [c.strip() for c in chart.split(",") if c.strip()]
    else:
        charts = list(DEFAULT_CHARTS)
    if charts:
        where += " AND " + in_list("KTOPL", charts)

    for field, value in (
        ("BKLAS", valuation_class),
        ("KOMOK", account_modifier), ("BWMOD", valuation_modifier),
    ):
        if value.strip():
            where += f" AND {field} = {abap_literal(value.strip().upper())}"

    rows = _all_rows(
        client, "T030", where,
        _existing(client, "T030", ["KTOPL", "KTOSL", "BWMOD", "KOMOK", "BKLAS", "KONTS", "KONTH"]),
    )

    # G/L account names, per chart of accounts, in IN-list batches.
    names: dict[tuple, str] = {}
    by_chart: dict[str, set] = {}
    for r in rows:
        for acct in (r.get("KONTS"), r.get("KONTH")):
            if acct:
                by_chart.setdefault(r.get("KTOPL", ""), set()).add(acct)
    for chart, accts in by_chart.items():
        accts_l = sorted(accts)
        for i in range(0, len(accts_l), 100):
            for t in _all_rows(
                client, "SKAT",
                f"SPRAS = {abap_literal(language)} AND KTOPL = {abap_literal(chart)} AND "
                + in_list("SAKNR", accts_l[i:i + 100]),
                ["KTOPL", "SAKNR", "TXT50"],
            ):
                names[(chart, t.get("SAKNR"))] = t.get("TXT50", "")

    key_text = ""
    try:
        text_field = next(iter(_existing(client, "T030W", ["LTEXT", "KTEXT", "TEXT", "TXT"])), "")
        if text_field:
            hit = _all_rows(
                client, "T030W",
                f"SPRAS = {abap_literal(language)} AND KTOSL = {abap_literal(ktosl)}",
                [text_field],
            )
            key_text = hit[0].get(text_field, "") if hit else ""
    except SapODataError:
        pass  # the label is a nicety; the determination itself matters

    columns = ["chart_of_accounts", "valuation_modifier", "account_modifier",
               "valuation_class", "debit_account", "credit_account"]
    out_rows = []
    # chart -> account -> name, each account listed once rather than per row.
    account_names: dict[str, dict[str, str]] = {}
    for r in rows:
        chart = r.get("KTOPL", "")
        row = {
            "chart_of_accounts": chart,
            "valuation_modifier": r.get("BWMOD", ""),
            "account_modifier": r.get("KOMOK", ""),
            "valuation_class": r.get("BKLAS", ""),
        }
        for src, dst in (("KONTS", "debit_account"), ("KONTH", "credit_account")):
            acct = r.get(src)
            if acct:
                row[dst] = _strip_zeros(acct)
                if names.get((chart, acct)):
                    account_names.setdefault(chart, {})[row[dst]] = names[(chart, acct)]
        out_rows.append(row)
    out_rows.sort(key=lambda r: tuple(str(r.get(k, "")) for k in columns[:4]))

    out: dict[str, Any] = {
        "transaction": ktosl,
        "transaction_text": key_text,
        "source": "T030 (OBYC)",
        "charts_of_accounts": charts or "all",
        "count": len(out_rows),
        **tabular(out_rows, columns),
        "account_names": account_names,
    }
    if not out_rows:
        out["note"] = f"No T030 entries for transaction key {ktosl} with these filters."
    return out
