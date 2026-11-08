"""MCP server entry point — read-only SAP S/4HANA OData connector."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
import threading
import time
from typing import Optional

from mcp.server.mcpserver import MCPServer

from . import __version__, attachments, compute, config_tables, printforms, reports, source, tool_text, usage
from . import credentials
from . import environments as envs
from .config import ConfigError, config_source, is_configured, load_config
from .sap_client import SapODataClient, SapODataError, SapTimeout, sap_error_text

log = logging.getLogger("sap_mcp")

#: Sent to Claude when it connects: how to choose between table reads and OData.
_INSTRUCTIONS = """SAP S/4HANA, read-only. Production by default; dev only via switch_environment.
PREFER TABLE READS: read_sap_table / aggregate_sap_table read any table or view like SE16, fast and exact, under the user's own authorizations. LET SAP DO THE ARITHMETIC: aggregate_sap_table, find_duplicates, trial_balance_by_period, open_items_ageing, grir_differences return results, not rows; never add up rows yourself.
SAP GUI REPORTS (find_sap_reports, sap_report_info, run_sap_report): when the user names a report or tcode; ask before any report that may post or change data.
OData (query_entity, get_service_metadata) only when a table read is refused, too large, or lacks SAP logic / derived fields.
WHERE = ABAP Open SQL: '1000', leading zeros ('0001010000'), dates '20260401'.
KEEP IT SMALL: big results go to Excel (you get a summary + path); ask for totals, only needed fields; the per-conversation SAP allowance can only be extended by the user.
Key tables: ACDOCA (RLDNR '0L', RBUKRS, GJAHR, POPER, BELNR, RACCT, HSL signed amount in company-code currency, BUDAT, PRCTR, KOSTL, LIFNR, KUNNR); BKPF/BSEG; BSID/BSAD, BSIK/BSAK (open/cleared AR/AP); SKA1/SKAT/SKB1; VBAK/VBAP, LIKP/LIPS, VBRK/VBRP; EKKO/EKPO/EKBE, RBKP/RSEG; MARA/MARC/MBEW, MATDOC; LFA1/KNA1/BUT000; T001, T030, T685/T683S.
DEVELOPMENT: get_sap_source reads ABAP code (program, include, class, function module, or a tcode); get_smartform shows a Smart Form's outline and saves its XML; read_sap_text reads SO10 texts; TNAPR (read_sap_table) maps output types to print programs and forms.
If old stand-alone SAP tools (mcp__sap__...) still appear: tell the user to restart Claude, then log in to SAP again."""
# Company specifics (company codes, chart of accounts, currency) come from the
# site settings, e.g. "Company: 1000 Example Ltd; chart of accounts INT; currency EUR."
_COMPANY_NOTE = str(envs.site_value("company_note", "") or "").strip()
if _COMPANY_NOTE:
    _INSTRUCTIONS += "\n" + _COMPANY_NOTE
mcp = MCPServer("sap-mcp", version=__version__, instructions=_INSTRUCTIONS)

#: mcp 2.x runs each (synchronous) tool in a worker thread, so tool calls can
#: overlap. This guards the module-level state below: the cached client, the
#: active environment and the session budget counters.
_state_lock = threading.RLock()

_client: Optional[SapODataClient] = None

#: SAP system every tool currently queries. Starts at SAP_ENV (default: prod)
#: and changes only via the switch_environment tool. Held in memory only, so a
#: restart of Claude Desktop always comes back to the configured default.
_active_env: str = envs.startup_env()


def _get_client() -> SapODataClient:
    global _client
    with _state_lock:
        if _client is None:
            _client = SapODataClient(load_config(_active_env))
        return _client


def _reset_client() -> None:
    """Drop the cached HTTP session so the next call re-reads the config."""
    global _client
    with _state_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # noqa: BLE001
                pass
        _client = None


def _env_tag() -> Optional[str]:
    """Marker added to every response from a non-production system.

    Production responses stay exactly as they were (no extra tokens). Anything
    from Development gets a loud label so its figures can't be mistaken for
    live books.
    """
    if envs.is_production(_active_env):
        return None
    m = envs.meta(_active_env)
    return (
        f"{_active_env.upper()} - {envs.describe(_active_env)}, client {m.get('client', '?')}. "
        "NOT production data."
    )


def _untagged(obj):
    """Inverse of :func:`_tagged` for re-wrapping an already tagged payload."""
    if isinstance(obj, dict) and "_sap_env" in obj:
        obj = {k: v for k, v in obj.items() if k != "_sap_env"}
    return obj


def _tagged(obj):
    """Prefix ``obj`` with the environment marker when not on production."""
    tag = _env_tag()
    if tag is None:
        return obj
    if isinstance(obj, dict):
        return {"_sap_env": tag, **obj}
    return {"_sap_env": tag, "data": obj}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1_000, int(os.environ[name]))
    except (KeyError, ValueError):
        return default


#: Hard per-answer ceiling. Rows above usage.INLINE_CHARS are saved to a file
#: first (usage.spill), so only results without a table can still hit this;
#: they are refused, never truncated (half a JSON document is useless).
_MAX_RESPONSE_CHARS = _env_int("SAP_MCP_MAX_RESPONSE_CHARS", 40_000)

#: Characters of SAP data per conversation before the user is asked
#: (~200k characters is roughly 50k tokens).
_SESSION_BUDGET_CHARS = _env_int("SAP_MCP_SESSION_BUDGET_CHARS", 200_000)

#: Characters returned to the caller so far this session (since the last
#: allowance the user approved).
_spent_chars = 0
#: Calls so far this session (since the last approved allowance).
_call_count = 0
#: Allowances the user approved beyond the first.
_extensions = 0
#: tool -> [calls, characters] for this session.
_by_tool: dict[str, list[int]] = {}


def _budget_state() -> dict:
    remaining = max(0, _SESSION_BUDGET_CHARS - _spent_chars)
    return {
        "spent_chars": _spent_chars,
        "budget_chars": _SESSION_BUDGET_CHARS,
        "remaining_chars": remaining,
        "approx_tokens_spent": _spent_chars // 4,
        "approx_tokens_remaining": remaining // 4,
        "percent_used": round(100 * _spent_chars / _SESSION_BUDGET_CHARS, 1),
        "calls": _call_count,
        "max_calls": usage.MAX_CALLS,
        "extensions_approved": _extensions,
    }


def _extend_allowance() -> bool:
    """Ask the user (window on screen) for another allowance; True if granted."""
    global _spent_chars, _call_count, _extensions
    if not usage.ask_to_continue(_budget_state()):
        return False
    with _state_lock:
        _spent_chars = 0
        _call_count = 0
        _extensions += 1
    return True


def _strip_odata_noise(obj):
    """Recursively drop OData v2 plumbing that carries no business meaning.

    Two things dominate a raw S/4HANA payload:

    * ``__metadata`` — attached to *every* entry, holding the full absolute
      URI of that row, repeated per row and per expanded child item.
    * ``__deferred`` — a stub emitted for every navigation property that was
      not expanded, each one another full URI. A purchase order header has a
      dozen of these.

    Neither can be turned off server-side, and together they are typically
    more than half the bytes of an expanded query. Stripping them here means
    every tool benefits, not just the ones we hand-tune.
    """
    if isinstance(obj, dict):
        cleaned = {}
        for key, value in obj.items():
            if key == "__metadata":
                continue
            # {"to_Item": {"__deferred": {"uri": "..."}}} — a link, not data.
            if isinstance(value, dict) and set(value) == {"__deferred"}:
                continue
            cleaned[key] = _strip_odata_noise(value)
        return cleaned
    if isinstance(obj, list):
        return [_strip_odata_noise(item) for item in obj]
    return obj


def _row_count(result) -> Optional[int]:
    """Best-effort row count for an OData response, for error messages."""
    if isinstance(result, dict):
        d = result.get("d")
        if isinstance(d, dict) and isinstance(d.get("results"), list):
            return len(d["results"])
    return None


#: Next step to suggest for each kind of SAP failure.
_SAP_HINTS = {
    0: "SAP could not be reached. Check the VPN / office network, then retry.",
    401: "Login rejected. Ask Claude to 'log in to SAP' (or 'log in to SAP dev') "
         "and enter your SAP password again.",
    403: "Your SAP user lacks authorization for this. Ask Basis to check the "
         "role (S_SERVICE for the service, plus the data-level objects).",
    404: "Service or entity not found. Check the name with list_services / "
         "get_service_metadata, and that the service is active in /IWFND/MAINT_SERVICE.",
}


def _safe(thunk) -> str:
    """Run one tool body with error handling and the usage controls."""
    tool = sys._getframe(1).f_code.co_name
    started = time.monotonic()
    out = _run_safely(thunk, tool)
    usage.log_call({
        "tool": tool, "env": _active_env, "chars": len(out),
        "ms": int(1000 * (time.monotonic() - started)),
        "saved_to_file": '"saved_to"' in out[:20_000] and '"row_count"' in out,
        "error": json.loads(out).get("error") if out.startswith('{"error"') else None,
    })
    return out


def _run_safely(thunk, tool: str) -> str:
    global _spent_chars, _call_count

    # Checked before the call, so a stop also saves the SAP round-trip. Only a
    # click by the user in the window extends the allowance.
    if (_spent_chars >= _SESSION_BUDGET_CHARS or _call_count >= usage.MAX_CALLS) and not _extend_allowance():
        return json.dumps(_tagged({
            "error": "budget_exhausted",
            "message": (
                f"This conversation has used its SAP allowance (~{_spent_chars // 4:,} tokens of "
                f"SAP data in {_call_count} calls) and the user did not approve more. No query "
                "was sent. Summarise what you have, or ask the user whether to continue."
            ),
            "budget": _budget_state(),
            "what_to_do": [
                "Start a new conversation for a new topic.",
                "Ask the user; on the next SAP call they will be asked again to allow more.",
            ],
        }))

    try:
        result = thunk()
    except ConfigError as e:
        return json.dumps(_tagged({"error": "config", "message": str(e)}))
    except SapTimeout as e:
        return json.dumps(_tagged({"error": "timeout", "message": str(e)}))
    except ValueError as e:
        return json.dumps(_tagged({"error": "invalid_argument", "message": str(e)}))
    except SapODataError as e:
        out = {"error": "sap", "status": e.status_code, "message": str(e)}
        hint = _SAP_HINTS.get(e.status_code)
        if hint:
            out["hint"] = hint
        # The readable part of the body is already in the message; only keep
        # the raw body when there was nothing readable to extract.
        if e.body and not sap_error_text(e.body):
            out["body"] = e.body[:1500]
        return json.dumps(_tagged(out))
    except Exception as e:  # noqa: BLE001
        return json.dumps(_tagged({"error": "internal", "message": repr(e)}))

    rows = _row_count(result)
    cleaned = _strip_odata_noise(result)
    payload = json.dumps(_tagged(cleaned), default=str)

    # Large tables go to Excel; the conversation gets a summary and the path.
    if len(payload) > usage.INLINE_CHARS:
        try:
            small = usage.spill(cleaned, tool, len(payload))
        except Exception as e:  # noqa: BLE001 - never lose the answer over the file
            small = None
            log.warning("could not save %s result to a file: %r", tool, e)
        if small is not None:
            payload = json.dumps(_tagged(small), default=str)

    if len(payload) > _MAX_RESPONSE_CHARS:
        return json.dumps(_tagged({
            "error": "too_large",
            "message": (
                f"Response is {len(payload):,} characters (~{len(payload) // 4:,} tokens)"
                + (f" across {rows} rows" if rows is not None else "")
                + f", over the {_MAX_RESPONSE_CHARS:,} per-call limit. It was NOT "
                "truncated — nothing was returned and nothing was charged against "
                "your budget, so re-run with a narrower query rather than retrying "
                "the same one."
            ),
            "how_to_narrow": [
                "Pass select= to ask for only the fields you need.",
                "Lower top= and page with skip=.",
                "Add or tighten a date range or other filter.",
                "If you set include_items=True, try it False first to size the result.",
            ],
            "rows_available": rows,
            "budget": _budget_state(),
        }))

    with _state_lock:
        _spent_chars += len(payload)
        _call_count += 1
        tally = _by_tool.setdefault(tool, [0, 0])
        tally[0] += 1
        tally[1] += len(payload)

    # Warn on the way past three-quarters, while there is still room to act.
    if _spent_chars > 0.75 * _SESSION_BUDGET_CHARS or _call_count > 0.75 * usage.MAX_CALLS:
        state = _budget_state()
        return json.dumps(_tagged({
            "_budget_warning": (
                f"{state['percent_used']}% of this conversation's SAP data allowance and "
                f"{state['calls']} of {state['max_calls']} calls are used. Prefer totals "
                "(aggregate_sap_table) and narrower queries; finish up soon."
            ),
            "_budget": state,
            "data": _untagged(json.loads(payload)),
        }))

    return payload


@mcp.tool()
def token_budget(reset: bool = False) -> str:
    """How much SAP data this conversation has pulled, per tool, and the limits.

    Free: does not touch SAP. ``reset=True`` asks the USER (a window on their
    screen) for another allowance; it cannot be granted without them.
    """
    if reset:
        granted = _extend_allowance()
        return json.dumps({"reset": granted, "budget": _budget_state(),
                           **({} if granted else {"message": "The user did not approve more SAP data."})})
    return json.dumps({
        **_budget_state(),
        "inline_limit_chars": usage.INLINE_CHARS,
        "by_tool": {k: {"calls": v[0], "approx_tokens": v[1] // 4}
                    for k, v in sorted(_by_tool.items(), key=lambda kv: -kv[1][1])},
        "usage_log": str(credentials.config_dir() / "usage_log.jsonl"),
    })


def _shape_catalog(payload: dict, search: str, limit: int, offset: int) -> dict:
    """Trim a raw catalog response down to what a caller can actually use.

    The Gateway catalog returns every service with a full OData ``__metadata``
    envelope attached to each entry. Left alone that is ~213k characters on a
    typical S/4HANA system — most of it the catalog URI repeated once per
    service. We keep three fields and drop the rest.
    """
    rows = (payload.get("d") or {}).get("results") or []

    services = [
        {
            "service_id": r.get("TechnicalServiceName") or r.get("ID") or "",
            "title": r.get("Title") or "",
            "version": r.get("TechnicalServiceVersion") or "",
        }
        for r in rows
    ]

    needle = (search or "").strip().lower()
    if needle:
        services = [
            s for s in services
            if needle in s["service_id"].lower() or needle in s["title"].lower()
        ]

    total = len(services)
    window = services[offset : offset + limit]

    out = {
        "total_in_catalog": len(rows),
        "total_matched": total,
        "returned": len(window),
        "offset": offset,
        "services": window,
    }
    if offset + len(window) < total:
        out["more_available"] = True
        out["hint"] = f"Call again with offset={offset + len(window)} for the next page."
    if needle and total == 0:
        out["hint"] = (
            f"No service matched {search!r}. Try a shorter term, or call with "
            "no search to page through the full catalog."
        )
    return out


@mcp.tool()
def list_services(search: str = "", limit: int = 50, offset: int = 0) -> str:
    """Search the SAP Gateway catalog for OData services.

    Returns only service_id, title and version — enough to pick a service and
    pass it to get_service_metadata. Always prefer a search term over paging
    through everything.

    Args:
        search: Case-insensitive substring matched against service name and
            title, e.g. ``"business partner"`` or ``"purchase"``. Empty
            returns the first page of the whole catalog.
        limit: Maximum services to return (default 50).
        offset: Skip this many matches — use for paging.
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return _safe(
        lambda: _shape_catalog(_get_client().list_services(), search, limit, offset)
    )


@mcp.tool()
def get_service_metadata(
    service: str,
    entity: Optional[str] = None,
    raw: bool = False,
    refresh: bool = False,
) -> str:
    """Inspect an OData service's structure.

    With no ``entity``, returns the list of entity sets in the service. With
    an ``entity``, returns that entity's keys and fields. Both are small.
    The service's $metadata is cached on disk for 7 days after the first
    read, so repeat questions about the same service are instant.

    Args:
        service: Service path segment, e.g. ``API_BUSINESS_PARTNER``.
        entity: Optional entity set or entity type name, e.g. ``A_Supplier``.
        raw: Return the complete raw EDMX document instead. This is very
            large — tens of thousands of tokens — so only set it when the
            parsed views are genuinely insufficient.
        refresh: Ignore the cache and download $metadata again (use after
            the service was changed in SAP).
    """
    if raw:
        return _safe(lambda: _get_client().get_metadata(service, refresh))
    if entity:
        return _safe(lambda: _get_client().get_entity_schema(service, entity, refresh))
    return _safe(lambda: _get_client().list_entity_sets(service, refresh))


@mcp.tool()
def query_entity(
    service: str,
    entity_set: str,
    select: Optional[str] = None,
    filter: Optional[str] = None,  # noqa: A002
    orderby: Optional[str] = None,
    top: Optional[int] = 50,
    skip: Optional[int] = None,
    expand: Optional[str] = None,
) -> str:
    """Generic OData v2 query. Use for anything not covered below."""
    return _safe(
        lambda: _get_client().query(
            service, entity_set,
            select=select, filter=filter, orderby=orderby,
            top=top, skip=skip, expand=expand,
        )
    )


@mcp.tool()
def gl_line_items(
    company_code: str,
    fiscal_year: str,
    posting_date_from: Optional[str] = None,
    posting_date_to: Optional[str] = None,
    gl_account: Optional[str] = None,
    top: int = 100,
) -> str:
    """GL line items via API_OPLACCTGDOCITEMCUBE_SRV."""
    filters = [
        f"CompanyCode eq '{company_code}'",
        f"FiscalYear eq '{fiscal_year}'",
    ]
    if posting_date_from:
        filters.append(f"PostingDate ge datetime'{posting_date_from}T00:00:00'")
    if posting_date_to:
        filters.append(f"PostingDate le datetime'{posting_date_to}T23:59:59'")
    if gl_account:
        filters.append(f"GLAccount eq '{gl_account}'")
    return _safe(
        lambda: _get_client().query(
            # Entity set discovered via service document probe on a live S/4HANA 2023
            # system. Earlier guess "YY1_GLAccountLineItems" was a CDS-view-style name
            # that may exist in some installations as a custom analytical view but is
            # not the standard SAP-shipped entity for this service.
            "API_OPLACCTGDOCITEMCUBE_SRV", "A_OperationalAcctgDocItemCube",
            filter=" and ".join(filters), orderby="PostingDate desc", top=top,
        )
    )


# ---------------------------------------------------------------------------
# Balance / trial-balance tools.
#
# These aggregate GL line items down to a balance in the tool layer, so callers
# get a single number instead of every row. The cube is OData v2 (no server-side
# $apply=aggregate), so rows are summed client-side. AmountInCompanyCodeCurrency
# is already signed (debit +, credit -), so the net balance is sum(amount);
# per-side totals split on DebitCreditCode (S=Debit, H=Credit) using abs(amount).
# ---------------------------------------------------------------------------

_BAL_SERVICE = "API_OPLACCTGDOCITEMCUBE_SRV"
_BAL_ENTITY = "A_OperationalAcctgDocItemCube"
_BAL_SELECT = (
    "CompanyCode,GLAccount,GLAccountName,FiscalYear,FiscalPeriod,PostingDate,"
    "DebitCreditCode,AmountInCompanyCodeCurrency,CompanyCodeCurrency,"
    "IsBalanceSheetAccount,ProfitLossAccountType"
)


def _bal_filters(
    company_code: str,
    gl_account: Optional[str] = None,
    fiscal_year: Optional[str] = None,
    posting_date_from: Optional[str] = None,
    posting_date_to: Optional[str] = None,
    gl_account_from: Optional[str] = None,
    gl_account_to: Optional[str] = None,
    profit_center: Optional[str] = None,
) -> str:
    f = [f"CompanyCode eq '{company_code}'"]
    if fiscal_year:
        f.append(f"FiscalYear eq '{fiscal_year}'")
    if gl_account:
        f.append(f"GLAccount eq '{gl_account}'")
    if gl_account_from:
        f.append(f"GLAccount ge '{gl_account_from}'")
    if gl_account_to:
        f.append(f"GLAccount le '{gl_account_to}'")
    if posting_date_from:
        f.append(f"PostingDate ge datetime'{posting_date_from}T00:00:00'")
    if posting_date_to:
        f.append(f"PostingDate le datetime'{posting_date_to}T23:59:59'")
    if profit_center:
        f.append(f"ProfitCenter eq '{profit_center}'")
    return " and ".join(f)


def _to_float(v) -> float:
    """SAP returns Edm.Decimal values as JSON strings to avoid precision loss."""
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@mcp.tool()
def gl_account_balance(
    company_code: str,
    gl_account: str,
    as_of_date: Optional[str] = None,
    from_date: Optional[str] = None,
    fiscal_year: Optional[str] = None,
    profit_center: Optional[str] = None,
    max_lines: int = 200_000,
) -> str:
    """Closing (or period-movement) balance for a single GL account.

    Returns the *net signed balance* in company code currency, plus the
    debit total, credit total, and the number of line items aggregated.
    Use this instead of ``gl_line_items`` whenever the question is "what is
    the balance" — gl_line_items dumps every row into the model's context,
    which is wasteful and slow for active accounts.

    Args:
        company_code: SAP company code, e.g. ``"1000"``.
        gl_account: GL account number, e.g. ``"4001099"``.
        as_of_date: ``YYYY-MM-DD``. Include all postings on or before this
            date. If omitted, includes everything available.
        from_date: ``YYYY-MM-DD``. If supplied alongside ``as_of_date``,
            you get the *period movement* (postings within the window)
            instead of the cumulative balance to ``as_of_date``.
        fiscal_year: Optional 4-digit fiscal year filter, e.g. ``"2025"``.
            Useful both for performance (smaller scan) and to scope a
            closing balance to a specific year.
        profit_center: Optional profit center filter.
        max_lines: Safety cap on line items to read (default 200k). If
            the cap is hit the response sets ``truncated: true`` and the
            balance is the partial sum — re-run with a tighter date range.
    """
    def _do():
        client = _get_client()
        flt = _bal_filters(
            company_code=company_code,
            gl_account=gl_account,
            fiscal_year=fiscal_year,
            posting_date_from=from_date,
            posting_date_to=as_of_date,
            profit_center=profit_center,
        )
        debits = 0.0
        credits = 0.0
        line_count = 0
        currency = ""
        account_name = ""
        for row in client.query_all(
            _BAL_SERVICE, _BAL_ENTITY,
            select=_BAL_SELECT, filter=flt,
            orderby="PostingDate asc",
            page_size=1000, max_records=max_lines,
        ):
            amt = _to_float(row.get("AmountInCompanyCodeCurrency"))
            dc = row.get("DebitCreditCode", "")
            # Use absolute amount for the per-side totals; the sign on the
            # row already encodes the side, so abs() gives the gross figure.
            if dc == "S":
                debits += abs(amt)
            elif dc == "H":
                credits += abs(amt)
            line_count += 1
            if not currency:
                currency = row.get("CompanyCodeCurrency", "") or ""
            if not account_name:
                account_name = row.get("GLAccountName", "") or ""
        truncated = line_count >= max_lines
        balance = round(debits - credits, 2)
        return {
            "company_code": company_code,
            "gl_account": gl_account,
            "account_name": account_name,
            "currency": currency,
            "as_of_date": as_of_date,
            "from_date": from_date,
            "fiscal_year": fiscal_year,
            "profit_center": profit_center,
            "debits_total": round(debits, 2),
            "credits_total": round(credits, 2),
            "balance": balance,
            "balance_side": "DR" if balance >= 0 else "CR",
            "line_count": line_count,
            "truncated": truncated,
            "source_entity": f"{_BAL_SERVICE}/{_BAL_ENTITY}",
        }
    return _safe(_do)


@mcp.tool()
def trial_balance(
    company_code: str,
    as_of_date: Optional[str] = None,
    from_date: Optional[str] = None,
    fiscal_year: Optional[str] = None,
    gl_account_from: Optional[str] = None,
    gl_account_to: Optional[str] = None,
    profit_center: Optional[str] = None,
    only_nonzero: bool = True,
    max_lines: int = 500_000,
) -> str:
    """Trial balance: per-GL-account debits/credits/net for a date range.

    Returns one row per GL account in the filter range, with the same fields
    as :func:`gl_account_balance` but aggregated per account. Use this for
    Excel reconciliation, balance sheet roll-ups, or quick "what moved last
    month" reviews.

    Args:
        company_code: SAP company code.
        as_of_date: ``YYYY-MM-DD``. Posting date cutoff (inclusive).
        from_date: ``YYYY-MM-DD``. If set, you get period movement rather
            than cumulative balances.
        fiscal_year: Optional fiscal year filter (recommended for speed).
        gl_account_from / gl_account_to: Range of GL accounts to include.
            Both inclusive. Useful for "asset accounts only" type cuts.
        profit_center: Optional profit center filter.
        only_nonzero: If True (default) drop accounts whose net balance
            and gross movement are both zero — keeps the response small.
        max_lines: Safety cap on line items scanned across all accounts.
    """
    def _do():
        client = _get_client()
        flt = _bal_filters(
            company_code=company_code,
            fiscal_year=fiscal_year,
            posting_date_from=from_date,
            posting_date_to=as_of_date,
            gl_account_from=gl_account_from,
            gl_account_to=gl_account_to,
            profit_center=profit_center,
        )
        # account_key -> aggregates
        accts: dict[str, dict] = {}
        line_count = 0
        currency = ""
        for row in client.query_all(
            _BAL_SERVICE, _BAL_ENTITY,
            select=_BAL_SELECT, filter=flt,
            orderby="GLAccount asc",
            page_size=1000, max_records=max_lines,
        ):
            gla = row.get("GLAccount") or ""
            if not gla:
                continue
            entry = accts.setdefault(gla, {
                "gl_account": gla,
                "account_name": row.get("GLAccountName", "") or "",
                "is_balance_sheet": bool(row.get("IsBalanceSheetAccount")),
                "pl_account_type": row.get("ProfitLossAccountType", "") or "",
                "debits_total": 0.0,
                "credits_total": 0.0,
                "line_count": 0,
            })
            amt = _to_float(row.get("AmountInCompanyCodeCurrency"))
            dc = row.get("DebitCreditCode", "")
            if dc == "S":
                entry["debits_total"] += abs(amt)
            elif dc == "H":
                entry["credits_total"] += abs(amt)
            entry["line_count"] += 1
            line_count += 1
            if not currency:
                currency = row.get("CompanyCodeCurrency", "") or ""
        truncated = line_count >= max_lines
        rows = []
        total_d = total_c = 0.0
        for entry in accts.values():
            entry["debits_total"] = round(entry["debits_total"], 2)
            entry["credits_total"] = round(entry["credits_total"], 2)
            entry["balance"] = round(entry["debits_total"] - entry["credits_total"], 2)
            entry["balance_side"] = "DR" if entry["balance"] >= 0 else "CR"
            if only_nonzero and entry["balance"] == 0 and entry["debits_total"] == 0 and entry["credits_total"] == 0:
                continue
            rows.append(entry)
            total_d += entry["debits_total"]
            total_c += entry["credits_total"]
        rows.sort(key=lambda r: r["gl_account"])
        return {
            "company_code": company_code,
            "currency": currency,
            "as_of_date": as_of_date,
            "from_date": from_date,
            "fiscal_year": fiscal_year,
            "gl_account_from": gl_account_from,
            "gl_account_to": gl_account_to,
            "profit_center": profit_center,
            "account_count": len(rows),
            "total_debits": round(total_d, 2),
            "total_credits": round(total_c, 2),
            "total_net": round(total_d - total_c, 2),
            "scanned_line_count": line_count,
            "truncated": truncated,
            "rows": rows,
            "source_entity": f"{_BAL_SERVICE}/{_BAL_ENTITY}",
        }
    return _safe(_do)


# ---------------------------------------------------------------------------
# Sales / billing tools.
#
# Per-material sales volume and value from the billing documents. Daily / MTD /
# YTD slices pick the right date window and aggregate client-side (the billing
# service is OData v2 and doesn't accept $apply=groupby/aggregate). The YTD
# helper defaults to an Apr–Mar fiscal year; override via fiscal_year_start_month.
# ---------------------------------------------------------------------------

_SLS_SERVICE = "API_BILLING_DOCUMENT_SRV"
_SLS_HEADER_ENTITY = "A_BillingDocument"

# Important schema note (verified live against S/4HANA 2023):
# A_BillingDocumentItem does NOT carry header fields like BillingDocumentDate,
# CompanyCode, SoldToParty, SalesOrganization, or DistributionChannel. Those
# only exist on the header A_BillingDocument. Naive item-level filtering on
# those fields returns HTTP 400 "Property X not found in type
# A_BillingDocumentItemType". The correct pattern is to filter at the header
# level and use $expand=to_Item to pull the items inline per header.
#
# Header fields we select (just what aggregation needs — full row would be
# wasteful on large date ranges):
_SLS_HEADER_SELECT = (
    "BillingDocument,BillingDocumentDate,CompanyCode,"
    "SalesOrganization,DistributionChannel,Division,"
    "SoldToParty,PayerParty,TransactionCurrency,"
    "BillingDocumentIsCancelled,OverallBillingStatus,AccountingPostingStatus,"
    "to_Item"
)


def _sls_header_filters(
    company_code: Optional[str] = None,
    sales_organization: Optional[str] = None,
    distribution_channel: Optional[str] = None,
    sold_to_party: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_cancelled: bool = False,
) -> str:
    """Build the $filter for A_BillingDocument header queries.

    Item-level filters (material, material_group, plant) are NOT here —
    those are applied client-side after expanding to_Item, because they
    are item properties.
    """
    f = []
    if company_code:
        f.append(f"CompanyCode eq '{company_code}'")
    if sales_organization:
        f.append(f"SalesOrganization eq '{sales_organization}'")
    if distribution_channel:
        f.append(f"DistributionChannel eq '{distribution_channel}'")
    if sold_to_party:
        f.append(f"SoldToParty eq '{sold_to_party}'")
    if date_from:
        f.append(f"BillingDocumentDate ge datetime'{date_from}T00:00:00'")
    if date_to:
        f.append(f"BillingDocumentDate le datetime'{date_to}T23:59:59'")
    if not include_cancelled:
        # Exclude cancellation documents AND cancelled originals so totals
        # are net of returns/cancellations. BillingDocumentIsCancelled flags
        # the original that has been cancelled; cancellation docs themselves
        # carry negative quantities/amounts so they would otherwise net out
        # anyway, but excluding them explicitly is cleaner for counts.
        f.append("BillingDocumentIsCancelled eq false")
    return " and ".join(f) if f else ""


def _fiscal_year_start(today: _dt.date, fy_start_month: int) -> _dt.date:
    """First day of the fiscal year that ``today`` belongs to.

    For an India-default of fy_start_month=4 (April), FY runs Apr 1 → Mar 31.
    A date of 2026-05-30 returns 2026-04-01; 2026-02-15 returns 2025-04-01.
    """
    fy_start_month = max(1, min(12, fy_start_month))
    if today.month >= fy_start_month:
        return _dt.date(today.year, fy_start_month, 1)
    return _dt.date(today.year - 1, fy_start_month, 1)


def _do_sales_by_material(
    *,
    date_from: str,
    date_to: str,
    company_code: Optional[str],
    sales_organization: Optional[str],
    distribution_channel: Optional[str],
    plant: Optional[str],
    material: Optional[str],
    material_group: Optional[str],
    sold_to_party: Optional[str],
    include_cancelled: bool = False,
    max_headers: int = 50_000,
) -> dict:
    """Shared aggregation engine for all sales tools below.

    Implementation note: queries the header entity A_BillingDocument with
    $expand=to_Item and aggregates the embedded items in Python. This is the
    only portable pattern for material-level analytics on this OData v2
    service because (a) header fields like BillingDocumentDate / CompanyCode
    aren't projected on A_BillingDocumentItem, so item-level filtering on
    those fields fails with HTTP 400, and (b) $apply=groupby/aggregate (the
    OData v4 way) isn't supported on this entity.

    Page size is intentionally smaller (200 vs the usual 1000) because each
    expanded header carries N items, so response payloads grow N-fold. 200
    keeps pages well under Gateway response limits for typical billing
    docs (avg 1-10 items per invoice).
    """
    client = _get_client()
    flt = _sls_header_filters(
        company_code=company_code,
        sales_organization=sales_organization,
        distribution_channel=distribution_channel,
        sold_to_party=sold_to_party,
        date_from=date_from,
        date_to=date_to,
        include_cancelled=include_cancelled,
    )

    # material -> aggregate row
    mats: dict[str, dict] = {}
    header_count = 0
    item_count = 0
    currency = ""
    invoice_set: set[str] = set()
    contributing_invoices: set[str] = set()  # invoices that had >=1 matching item

    for header in client.query_all(
        _SLS_SERVICE, _SLS_HEADER_ENTITY,
        select=_SLS_HEADER_SELECT, filter=flt or None,
        expand="to_Item",
        # Sort by date so paging is deterministic; doesn't change the totals.
        orderby="BillingDocumentDate asc,BillingDocument asc",
        page_size=200, max_records=max_headers,
    ):
        bd = header.get("BillingDocument") or ""
        if bd:
            invoice_set.add(bd)
        header_count += 1
        if not currency:
            currency = header.get("TransactionCurrency", "") or ""

        # OData v2 expand returns the children under "results"
        items = (header.get("to_Item", {}) or {}).get("results", []) or []
        had_match = False
        for it in items:
            # Apply item-level filters in Python (these fields don't exist on
            # the header so we can't push them into $filter).
            mat = it.get("Material") or ""
            if not mat:
                continue
            if material and mat != material:
                continue
            if material_group and (it.get("MaterialGroup", "") or "") != material_group:
                continue
            if plant and (it.get("Plant", "") or "") != plant:
                continue

            entry = mats.setdefault(mat, {
                "material": mat,
                "material_group": it.get("MaterialGroup", "") or "",
                "quantity": 0.0,
                "quantity_unit": it.get("BillingQuantityUnit", "") or "",
                "net_value": 0.0,
                "currency": it.get("TransactionCurrency", "") or "",
                "invoice_count": 0,
                "_invoice_set": set(),
            })
            qty = _to_float(it.get("BillingQuantity"))
            net = _to_float(it.get("NetAmount"))
            entry["quantity"] += qty
            entry["net_value"] += net
            if bd:
                entry["_invoice_set"].add(bd)
            # Mixed UOM detection: if a material has been billed in different
            # units within the window the summed quantity is meaningless;
            # flag it so the caller knows.
            u = it.get("BillingQuantityUnit", "") or ""
            if u and entry["quantity_unit"] and u != entry["quantity_unit"]:
                entry["quantity_unit"] = "MIXED"
            item_count += 1
            had_match = True
        if had_match and bd:
            contributing_invoices.add(bd)

    truncated = header_count >= max_headers
    rows = []
    total_qty = 0.0
    total_net = 0.0
    for entry in mats.values():
        entry["quantity"] = round(entry["quantity"], 3)
        entry["net_value"] = round(entry["net_value"], 2)
        entry["invoice_count"] = len(entry.pop("_invoice_set"))
        rows.append(entry)
        total_qty += entry["quantity"]
        total_net += entry["net_value"]
    rows.sort(key=lambda r: -r["net_value"])  # biggest revenue first

    return {
        "company_code": company_code,
        "sales_organization": sales_organization,
        "date_from": date_from,
        "date_to": date_to,
        "currency": currency,
        "material_count": len(rows),
        "invoice_count": len(contributing_invoices),
        "billing_documents_scanned": len(invoice_set),
        "total_quantity": round(total_qty, 3),
        "total_net_value": round(total_net, 2),
        "scanned_header_count": header_count,
        "scanned_item_count": item_count,
        "truncated": truncated,
        "rows": rows,
        "source_entity": f"{_SLS_SERVICE}/{_SLS_HEADER_ENTITY}+to_Item",
    }


@mcp.tool()
def sales_by_material(
    date_from: str,
    date_to: str,
    company_code: Optional[str] = None,
    sales_organization: Optional[str] = None,
    distribution_channel: Optional[str] = None,
    plant: Optional[str] = None,
    material: Optional[str] = None,
    material_group: Optional[str] = None,
    sold_to_party: Optional[str] = None,
    include_cancelled: bool = False,
    max_headers: int = 50_000,
) -> str:
    """Per-material invoiced sales (volume + net value) for a date window.

    Aggregates billing data by ``Material``. Returns one row per material
    with summed ``BillingQuantity`` (volume) and ``NetAmount`` (value), plus
    the count of distinct billing documents that contributed. Use this for
    "what products did we sell, and for how much" questions.

    Cancelled billing documents are excluded by default so the totals match
    P&L-relevant invoiced revenue. Credit memos that are still active appear
    as negative quantities/values, so the totals are net of returns.

    Implementation: queries the header ``A_BillingDocument`` with date /
    company / sales-org filters and ``$expand=to_Item``, then aggregates
    items in Python. Item-level filters (``material``, ``material_group``,
    ``plant``) are applied client-side because those fields don't exist on
    the header.

    Args:
        date_from / date_to: ``YYYY-MM-DD``. Filters BillingDocumentDate
            inclusive on both ends.
        company_code: Filter by SAP company code, e.g. ``"1000"``.
        sales_organization / distribution_channel: Optional SD cuts.
        plant: Restrict to one plant (item-level filter).
        material: Restrict to a single material number.
        material_group: Restrict to a material group, e.g. ``"B0009"``.
        sold_to_party: Restrict to one customer.
        include_cancelled: If True, include cancelled billing documents in
            the totals. Default False (recommended).
        max_headers: Safety cap on billing documents scanned (default 50k —
            with avg 5 items per doc that's 250k items). If hit,
            ``truncated: true`` — narrow the date range or apply filters.
    """
    return _safe(lambda: _do_sales_by_material(
        date_from=date_from, date_to=date_to,
        company_code=company_code,
        sales_organization=sales_organization,
        distribution_channel=distribution_channel,
        plant=plant, material=material,
        material_group=material_group,
        sold_to_party=sold_to_party,
        include_cancelled=include_cancelled,
        max_headers=max_headers,
    ))


@mcp.tool()
def sales_today(
    company_code: Optional[str] = None,
    sales_organization: Optional[str] = None,
    material: Optional[str] = None,
    material_group: Optional[str] = None,
    max_headers: int = 10_000,
) -> str:
    """Per-material invoiced sales for today (the local server date).

    Convenience wrapper around :func:`sales_by_material` with
    ``date_from == date_to == today``. Useful for end-of-day or
    intraday "what have we billed today" snapshots.
    """
    today = _dt.date.today().isoformat()
    return _safe(lambda: {
        "scope": "today",
        "as_of": today,
        **_do_sales_by_material(
            date_from=today, date_to=today,
            company_code=company_code,
            sales_organization=sales_organization,
            distribution_channel=None, plant=None,
            material=material, material_group=material_group,
            sold_to_party=None, max_headers=max_headers,
        ),
    })


@mcp.tool()
def sales_mtd(
    company_code: Optional[str] = None,
    sales_organization: Optional[str] = None,
    material: Optional[str] = None,
    material_group: Optional[str] = None,
    max_headers: int = 20_000,
) -> str:
    """Per-material invoiced sales month-to-date (first of this month → today).

    Convenience wrapper around :func:`sales_by_material`. The window is
    derived from the local server date — no fiscal calendar needed since
    "month" is universal.
    """
    today = _dt.date.today()
    first = today.replace(day=1)
    return _safe(lambda: {
        "scope": "mtd",
        "month": first.strftime("%Y-%m"),
        "as_of": today.isoformat(),
        **_do_sales_by_material(
            date_from=first.isoformat(), date_to=today.isoformat(),
            company_code=company_code,
            sales_organization=sales_organization,
            distribution_channel=None, plant=None,
            material=material, material_group=material_group,
            sold_to_party=None, max_headers=max_headers,
        ),
    })


@mcp.tool()
def sales_ytd(
    company_code: Optional[str] = None,
    sales_organization: Optional[str] = None,
    material: Optional[str] = None,
    material_group: Optional[str] = None,
    fiscal_year_start_month: int = 4,
    max_headers: int = 100_000,
) -> str:
    """Per-material invoiced sales year-to-date (first day of fiscal year → today).

    Convenience wrapper around :func:`sales_by_material`. Defaults to the
    Indian fiscal year (April–March). For a calendar-year YTD, pass
    ``fiscal_year_start_month=1``.

    Args:
        fiscal_year_start_month: 1–12. The month that fiscal year starts on.
            Default 4 (April) reflects the Indian tax year.
        max_headers: Cap on billing documents scanned. Default 100k handles
            a full fiscal year for a mid-size manufacturer.
    """
    today = _dt.date.today()
    fy_start = _fiscal_year_start(today, fiscal_year_start_month)
    return _safe(lambda: {
        "scope": "ytd",
        "fiscal_year_start": fy_start.isoformat(),
        "as_of": today.isoformat(),
        **_do_sales_by_material(
            date_from=fy_start.isoformat(), date_to=today.isoformat(),
            company_code=company_code,
            sales_organization=sales_organization,
            distribution_channel=None, plant=None,
            material=material, material_group=material_group,
            sold_to_party=None, max_headers=max_headers,
        ),
    })


@mcp.tool()
def journal_entries(
    company_code: str,
    fiscal_year: str,
    gl_account: Optional[str] = None,
    profit_center: Optional[str] = None,
    fiscal_period: Optional[str] = None,
    top: int = 100,
) -> str:
    """Journal entry items via API_JOURNALENTRYITEMBASIC_SRV.

    NOTE: This is the "Basic" projection of journal entries — it is
    aggregated/summarised by ledger / period / account / cost object.
    It deliberately does NOT expose per-document fields such as
    AccountingDocument number, PostingDate, or CreatedByUser. For
    document-level detail, call :func:`gl_line_items` instead, which
    targets API_OPLACCTGDOCITEMCUBE_SRV.

    The fiscal year filter uses ``LedgerFiscalYear`` (the entity's
    actual field name); ``fiscal_year`` is the value, e.g. ``"2024"``.
    The ``fiscal_period`` argument, if supplied, should be the numeric
    period like ``"6"``.
    """
    filters = [
        f"CompanyCode eq '{company_code}'",
        f"LedgerFiscalYear eq '{fiscal_year}'",
    ]
    if gl_account:
        filters.append(f"GLAccount eq '{gl_account}'")
    if profit_center:
        filters.append(f"ProfitCenter eq '{profit_center}'")
    if fiscal_period:
        filters.append(f"FiscalPeriod eq '{fiscal_period}'")
    return _safe(
        lambda: _get_client().query(
            "API_JOURNALENTRYITEMBASIC_SRV", "A_JournalEntryItemBasic",
            filter=" and ".join(filters),
            # PostingDate doesn't exist on this entity; sort by period within year.
            orderby="LedgerFiscalYear desc,FiscalPeriod desc",
            top=top,
        )
    )


@mcp.tool()
def vendor_master(
    search: Optional[str] = None,
    country: Optional[str] = None,
    top: int = 50,
) -> str:
    """Vendor / supplier master via API_BUSINESS_PARTNER/A_Supplier.

    Args:
        search: Case-sensitive substring of the supplier name. Matched against
            ``SupplierFullName``, which holds name plus city, so a term like
            ``"MARK FORGE"`` works. Names are stored uppercase in this tenant.
        country: Not available on A_Supplier — see note below.
        top: Max suppliers to return (default 50).
    """
    # A_Supplier has no Country field in this release (verified live against
    # Live tenant, 1 Sep 2026 — the entity has 38 properties and none of them is
    # Country). Address data lives on A_BusinessPartnerAddress. Fail with an
    # explanation rather than letting SAP return an opaque 400.
    if country:
        return json.dumps({
            "error": "unsupported_filter",
            "message": (
                "A_Supplier has no Country field in this system, so vendors "
                "cannot be filtered by country here."
            ),
            "alternative": (
                "Query A_BusinessPartnerAddress with query_entity and join on "
                "BusinessPartner, or filter the returned SupplierFullName, "
                "which usually carries the city."
            ),
        })

    filters = []
    if search:
        # BusinessPartnerName does not exist on A_SupplierType — using it was
        # a 400 on every call. SupplierFullName is the searchable name field.
        filters.append(f"substringof('{search}',SupplierFullName)")
    return _safe(
        lambda: _get_client().query(
            "API_BUSINESS_PARTNER", "A_Supplier",
            select=(
                "Supplier,SupplierName,SupplierFullName,SupplierAccountGroup,"
                "PurchasingIsBlocked,PostingIsBlocked,PaymentIsBlockedForSupplier,"
                "DeletionIndicator"
            ),
            filter=" and ".join(filters) if filters else None, top=top,
        )
    )


@mcp.tool()
def customer_master(
    search: Optional[str] = None,
    country: Optional[str] = None,
    top: int = 50,
) -> str:
    """Customer master via API_BUSINESS_PARTNER/A_Customer.

    Args:
        search: Case-sensitive substring matched against ``CustomerFullName``.
        country: Not available on A_Customer — see vendor_master.
        top: Max customers to return (default 50).
    """
    # Same two bugs as vendor_master: A_CustomerType has neither
    # BusinessPartnerName nor Country (52 properties, verified live 1 Sep 2026).
    if country:
        return json.dumps({
            "error": "unsupported_filter",
            "message": (
                "A_Customer has no Country field in this system, so customers "
                "cannot be filtered by country here."
            ),
            "alternative": (
                "Query A_BusinessPartnerAddress with query_entity and join on "
                "BusinessPartner."
            ),
        })

    filters = []
    if search:
        filters.append(f"substringof('{search}',CustomerFullName)")
    return _safe(
        lambda: _get_client().query(
            "API_BUSINESS_PARTNER", "A_Customer",
            filter=" and ".join(filters) if filters else None, top=top,
        )
    )


@mcp.tool()
def material_master(
    search: Optional[str] = None,
    material_type: Optional[str] = None,
    top: int = 50,
) -> str:
    """Material / product master via API_PRODUCT_SRV/A_Product."""
    filters = []
    if material_type:
        filters.append(f"ProductType eq '{material_type}'")
    if search:
        filters.append(f"substringof('{search}', Product)")
    return _safe(
        lambda: _get_client().query(
            "API_PRODUCT_SRV", "A_Product",
            filter=" and ".join(filters) if filters else None, top=top,
        )
    )


@mcp.tool()
def gst_register(
    company_code: str,
    posting_date_from: str,
    posting_date_to: str,
    register_type: str = "GSTR1",
    top: int = 200,
) -> str:
    """India GST register entries via ZGSTREGISTER_SRV (custom service).

    If your basis team hasn't activated this name, use list_services to find
    the right one, then call query_entity directly.
    """
    filters = [
        f"CompanyCode eq '{company_code}'",
        f"RegisterType eq '{register_type}'",
        f"PostingDate ge datetime'{posting_date_from}T00:00:00'",
        f"PostingDate le datetime'{posting_date_to}T23:59:59'",
    ]
    return _safe(
        lambda: _get_client().query(
            "ZGSTREGISTER_SRV", "GSTRegisterSet",
            filter=" and ".join(filters), orderby="PostingDate desc", top=top,
        )
    )


@mcp.tool()
def whoami() -> str:
    """Echo the active SAP environment, host, client and user (never the password)."""
    try:
        cfg = load_config(_active_env)
    except ConfigError as e:
        return json.dumps({
            "error": "config",
            "environment": _active_env,
            "system": envs.describe(_active_env),
            "message": str(e),
        })
    return json.dumps({
        "environment": cfg.env,
        "system": envs.describe(cfg.env),
        "is_production": envs.is_production(cfg.env),
        "base_url": cfg.base_url,
        "client": cfg.client,
        "username": cfg.username,
        "service_root": cfg.service_root,
        "verify_tls": cfg.verify_tls,
        "auth_source": config_source(cfg.env),
        "other_environments": [e for e in envs.names() if e != cfg.env],
    })


# ===========================================================================
# Environments (Production / Development). See sap_mcp.environments.
# ===========================================================================

def _env_summary(env: str) -> dict:
    """Connection details for ``env`` without touching SAP or the password."""
    m = envs.meta(env)
    out = {
        "environment": env,
        "system": envs.describe(env),
        "is_production": envs.is_production(env),
        "active": env == _active_env,
    }
    try:
        cfg = load_config(env)
        out.update(configured=True, base_url=cfg.base_url, client=cfg.client, username=cfg.username)
    except ConfigError:
        stored = credentials.load_stored(env) or {}
        out.update(
            configured=False,
            base_url=stored.get("base_url") or m.get("base_url"),
            client=stored.get("client") or m.get("client"),
            username=stored.get("username"),
            how_to_log_in=(
                "Call sap_login (opens a login window), or run: sap-mcp setup"
                if envs.is_production(env)
                else f"Call sap_login(env=\"{env}\") (opens a login window), "
                     f"or run: sap-mcp setup --env {env}"
            ),
        )
    return out


@mcp.tool()
def list_environments() -> str:
    """List the SAP systems this server can query (Production, Development),
    which one is active, and whether you are logged in to each. No SAP call."""
    return json.dumps({
        "active_environment": _active_env,
        "environments": [_env_summary(e) for e in envs.names()],
        "hint": "Call switch_environment('dev') or switch_environment('prod') to change systems.",
    })


@mcp.tool()
def switch_environment(env: str) -> str:
    """Point every SAP tool at a different SAP system.

    env: "prod" = Production - the default.
         "dev"  = Development / Testing.
         Also accepts "production", "development", "test", or the SID.

    The switch stays in effect for this Claude Desktop session (a restart goes
    back to Production). While on a non-production system every response
    carries a "_sap_env" marker - never report those figures as live books.
    Calling it for the current system re-reads the stored login (use after a
    password change). Makes no SAP call; if you are not logged in to the target
    system, nothing changes and you get login instructions instead.
    """
    global _active_env
    try:
        target = envs.normalize(env)
    except envs.UnknownEnvironment as e:
        return json.dumps({
            "error": "unknown_environment",
            "message": str(e),
            "active_environment": _active_env,
            "available": envs.names(),
        })

    if not is_configured(target):
        try:
            load_config(target)
            message = ""
        except ConfigError as e:
            message = str(e)
        return json.dumps({
            "error": "not_configured",
            "message": message,
            "active_environment": _active_env,
            "note": f"Still on {envs.describe(_active_env)} - nothing was switched.",
        })

    with _state_lock:
        previous = _active_env
        _reset_client()
        _active_env = target
    out = _env_summary(target)
    out.update(
        switched=previous != target,
        previous_environment=previous,
        note=(
            "Now querying PRODUCTION." if envs.is_production(target)
            else f"Now querying {envs.describe(target)}. This is NOT production data."
        ),
    )
    return json.dumps(out)



# ===========================================================================
# P2P (procure-to-pay) read tools. Strictly read-only.
#
# Dedicated wrappers over the released OData V2 procurement services, so callers
# don't have to hand-build query_entity calls. Entity/field names come from
# SAP's public API Business Hub docs; readers avoid forcing a curated $select
# (they return full rows) and expose select / filter / expand passthroughs, so
# fields flagged "confirm in-tenant" can be pinned without code changes.
#
#   Service                          Header / item entities (V2)
#   API_PURCHASEORDER_PROCESS_SRV    A_PurchaseOrder / A_PurchaseOrderItem / A_PurOrdScheduleLine
#   API_PURCHASEREQ_PROCESS_SRV      A_PurchaseRequisitionHeader / A_PurchaseRequisitionItem
#   API_MATERIAL_DOCUMENT_SRV        A_MaterialDocumentHeader / A_MaterialDocumentItem
#   API_SUPPLIERINVOICE_PROCESS_SRV  A_SupplierInvoice / A_SuplrInvcItemPurOrdRef
#   API_OPLACCTGDOCITEMCUBE_SRV      A_OperationalAcctgDocItemCube (vendor open items)
# ===========================================================================

_PO_SERVICE = "API_PURCHASEORDER_PROCESS_SRV"
_PO_HEADER_ENTITY = "A_PurchaseOrder"

#: A_PurchaseOrder carries 57 header fields; these are the ones anyone
#: actually reads off a PO. Callers wanting more pass select= explicitly.
#:
#: Every name below was verified against a live S/4HANA tenant on 1 Sep 2026 via
#: get_service_metadata. Note there is NO PurchaseOrderNetAmount on the header
#: in this release — a PO's value has to be summed from its items.
_PO_HEADER_SELECT = (
    "PurchaseOrder,PurchaseOrderType,PurchaseOrderDate,CompanyCode,"
    "Supplier,PurchasingGroup,PurchasingOrganization,DocumentCurrency,"
    "CreationDate,PurchasingDocumentDeletionCode,PurchasingProcessingStatus,"
    "PurchasingCompletenessStatus"
)

#: The expensive half. A_PurchaseOrderItem has exactly 100 fields, and they
#: multiply by the number of items on every header — which is what made
#: include_items=True so costly.
#:
#: Also verified live: there is no NetAmount on the item either. Line value
#: must be derived as OrderQuantity * NetPriceAmount / NetPriceQuantity,
#: because SAP quotes the price per NetPriceQuantity units, not per one.
_PO_ITEM_FIELDS = (
    "PurchaseOrderItem,Material,PurchaseOrderItemText,Plant,OrderQuantity,"
    "PurchaseOrderQuantityUnit,NetPriceAmount,NetPriceQuantity,DocumentCurrency,"
    "IsCompletelyDelivered,IsFinallyInvoiced,PurchasingDocumentDeletionCode"
)


@mcp.tool()
def purchase_orders(
    company_code: Optional[str] = None,
    supplier: Optional[str] = None,
    purchasing_group: Optional[str] = None,
    purchasing_organization: Optional[str] = None,
    purchase_order: Optional[str] = None,
    po_date_from: Optional[str] = None,
    po_date_to: Optional[str] = None,
    include_items: bool = False,
    select: Optional[str] = None,
    filter: Optional[str] = None,  # noqa: A002 — extra raw OData $filter, AND-ed in
    top: int = 100,
) -> str:
    """Purchase orders via API_PURCHASEORDER_PROCESS_SRV / A_PurchaseOrder. Read-only.

    Returns PO headers. Pass ``include_items=True`` to expand
    ``to_PurchaseOrderItem`` (item: Material, Plant, OrderQuantity,
    NetPriceAmount, IsCompletelyDelivered, IsFinallyInvoiced).

    Delivery-date caveat: the scheduled delivery date is **not** on the header.
    It lives on the schedule line
    (``A_PurOrdScheduleLine.ScheduleLineDeliveryDate``), reached via
    ``to_PurchaseOrderItem/to_ScheduleLine`` — read it with ``query_entity`` on
    the schedule-line entity once field names/version are confirmed in-tenant.

    Args:
        company_code: SAP company code, e.g. ``"1000"``.
        supplier: Supplier/vendor number (PO ``Supplier`` field).
        purchasing_group / purchasing_organization: SAP purchasing org units.
        purchase_order: A single PO number.
        po_date_from / po_date_to: ``YYYY-MM-DD`` on ``PurchaseOrderDate``.
        include_items: Expand ``to_PurchaseOrderItem`` when True.
        select / filter: Optional raw OData ``$select`` / extra ``$filter``
            (AND-ed onto the typed filters) for fields not exposed as args.
        top: Max headers (default 100).
    """
    f = []
    if company_code:
        f.append(f"CompanyCode eq '{company_code}'")
    if supplier:
        f.append(f"Supplier eq '{supplier}'")
    if purchasing_group:
        f.append(f"PurchasingGroup eq '{purchasing_group}'")
    if purchasing_organization:
        f.append(f"PurchasingOrganization eq '{purchasing_organization}'")
    if purchase_order:
        f.append(f"PurchaseOrder eq '{purchase_order}'")
    if po_date_from:
        f.append(f"PurchaseOrderDate ge datetime'{po_date_from}T00:00:00'")
    if po_date_to:
        f.append(f"PurchaseOrderDate le datetime'{po_date_to}T23:59:59'")
    if filter:
        f.append(f"({filter})")
    # Default to a curated field list. Without this SAP returns every header
    # field and — when items are expanded — every field of every item, which
    # is what made this tool expensive.
    if select:
        effective_select = select
    elif include_items:
        item_fields = ",".join(
            f"to_PurchaseOrderItem/{fld}" for fld in _PO_ITEM_FIELDS.split(",")
        )
        effective_select = f"{_PO_HEADER_SELECT},{item_fields}"
    else:
        effective_select = _PO_HEADER_SELECT

    return _safe(lambda: _get_client().query(
        _PO_SERVICE, _PO_HEADER_ENTITY,
        select=effective_select,
        filter=" and ".join(f) if f else None,
        orderby="PurchaseOrderDate desc",
        expand="to_PurchaseOrderItem" if include_items else None,
        top=top,
    ))


_PR_SERVICE = "API_PURCHASEREQ_PROCESS_SRV"
_PR_ITEM_ENTITY = "A_PurchaseRequisitionItem"


@mcp.tool()
def purchase_requisitions(
    plant: Optional[str] = None,
    purchasing_group: Optional[str] = None,
    material: Optional[str] = None,
    purchase_requisition: Optional[str] = None,
    requisition_date_from: Optional[str] = None,
    requisition_date_to: Optional[str] = None,
    select: Optional[str] = None,
    filter: Optional[str] = None,  # noqa: A002
    top: int = 100,
) -> str:
    """Purchase requisitions via API_PURCHASEREQ_PROCESS_SRV / A_PurchaseRequisitionItem. Read-only.

    Queries the item entity (it carries Plant, PurchasingGroup, Material).
    Release / approval state is partly workflow state: confirm the exact field
    in-tenant — candidates are ``PurchaseRequisitionRelease`` /
    ``PurReqnReleaseStatus`` / ``ProcessingStatus`` — and pass it through the
    ``filter`` arg, e.g. ``filter="ProcessingStatus eq '02'"``.

    Args:
        plant: Plant the requisition is for.
        purchasing_group: SAP purchasing group.
        material: Material number.
        purchase_requisition: A single PR number.
        requisition_date_from / requisition_date_to: ``YYYY-MM-DD`` on
            ``PurchaseRequisitionDate`` (confirm field name in-tenant).
        select / filter: Optional raw OData ``$select`` / extra ``$filter``.
        top: Max items (default 100).
    """
    f = []
    if plant:
        f.append(f"Plant eq '{plant}'")
    if purchasing_group:
        f.append(f"PurchasingGroup eq '{purchasing_group}'")
    if material:
        f.append(f"Material eq '{material}'")
    if purchase_requisition:
        f.append(f"PurchaseRequisition eq '{purchase_requisition}'")
    if requisition_date_from:
        f.append(f"PurchaseRequisitionDate ge datetime'{requisition_date_from}T00:00:00'")
    if requisition_date_to:
        f.append(f"PurchaseRequisitionDate le datetime'{requisition_date_to}T23:59:59'")
    if filter:
        f.append(f"({filter})")
    return _safe(lambda: _get_client().query(
        _PR_SERVICE, _PR_ITEM_ENTITY,
        select=select,
        filter=" and ".join(f) if f else None,
        top=top,
    ))


_MATDOC_SERVICE = "API_MATERIAL_DOCUMENT_SRV"
_MATDOC_HEADER_ENTITY = "A_MaterialDocumentHeader"


@mcp.tool()
def material_documents(
    posting_date_from: Optional[str] = None,
    posting_date_to: Optional[str] = None,
    material_document: Optional[str] = None,
    material_document_year: Optional[str] = None,
    plant: Optional[str] = None,
    material: Optional[str] = None,
    goods_movement_type: Optional[str] = None,
    purchase_order: Optional[str] = None,
    top: int = 100,
    max_headers: int = 20_000,
) -> str:
    """Goods receipts / material documents (GRN) via API_MATERIAL_DOCUMENT_SRV. Read-only.

    A goods receipt against a PO is movement type **101** (102 = reversal,
    122 = return to vendor). The material document records when the GR was
    *posted* (``PostingDate`` / ``CreationDate``), not any physical gate-entry
    time (SAP does not hold that).

    Implementation: queries the header (filtered on ``PostingDate``) and
    expands ``to_MaterialDocumentItem``; the item-level filters
    (``plant``, ``material``, ``goods_movement_type``, ``purchase_order``)
    are applied client-side after expand, because those are item properties.
    This mirrors the header+expand pattern used by ``sales_by_material``.

    Args:
        posting_date_from / posting_date_to: ``YYYY-MM-DD`` on ``PostingDate``.
        material_document / material_document_year: A single document.
        plant / material / goods_movement_type / purchase_order: Item-level
            filters applied client-side.
        top: Max headers per page hint.
        max_headers: Safety cap on documents scanned (default 20k).
    """
    def _do():
        client = _get_client()
        f = []
        if posting_date_from:
            f.append(f"PostingDate ge datetime'{posting_date_from}T00:00:00'")
        if posting_date_to:
            f.append(f"PostingDate le datetime'{posting_date_to}T23:59:59'")
        if material_document:
            f.append(f"MaterialDocument eq '{material_document}'")
        if material_document_year:
            f.append(f"MaterialDocumentYear eq '{material_document_year}'")
        want_item_filter = any([plant, material, goods_movement_type, purchase_order])
        out_headers = []
        scanned = 0
        for hdr in client.query_all(
            _MATDOC_SERVICE, _MATDOC_HEADER_ENTITY,
            filter=" and ".join(f) if f else None,
            expand="to_MaterialDocumentItem",
            orderby="PostingDate desc",
            page_size=200, max_records=max_headers,
        ):
            scanned += 1
            items = (hdr.get("to_MaterialDocumentItem", {}) or {}).get("results", []) or []
            if want_item_filter:
                kept = []
                for it in items:
                    if plant and (it.get("Plant", "") or "") != plant:
                        continue
                    if material and (it.get("Material", "") or "") != material:
                        continue
                    if goods_movement_type and (it.get("GoodsMovementType", "") or "") != goods_movement_type:
                        continue
                    if purchase_order and (it.get("PurchaseOrder", "") or "") != purchase_order:
                        continue
                    kept.append(it)
                if not kept:
                    continue
                hdr = dict(hdr)
                hdr["to_MaterialDocumentItem"] = {"results": kept}
            out_headers.append(hdr)
            if len(out_headers) >= top:
                break
        return {
            "posting_date_from": posting_date_from,
            "posting_date_to": posting_date_to,
            "filters": {
                "plant": plant, "material": material,
                "goods_movement_type": goods_movement_type,
                "purchase_order": purchase_order,
            },
            "document_count": len(out_headers),
            "scanned_header_count": scanned,
            "truncated": scanned >= max_headers,
            "documents": out_headers,
            "source_entity": f"{_MATDOC_SERVICE}/{_MATDOC_HEADER_ENTITY}+to_MaterialDocumentItem",
        }
    return _safe(_do)


_SINV_SERVICE = "API_SUPPLIERINVOICE_PROCESS_SRV"
_SINV_HEADER_ENTITY = "A_SupplierInvoice"


@mcp.tool()
def supplier_invoices(
    company_code: Optional[str] = None,
    invoicing_party: Optional[str] = None,
    fiscal_year: Optional[str] = None,
    posting_date_from: Optional[str] = None,
    posting_date_to: Optional[str] = None,
    payment_blocked_only: bool = False,
    supplier_invoice: Optional[str] = None,
    include_po_items: bool = False,
    select: Optional[str] = None,
    filter: Optional[str] = None,  # noqa: A002
    top: int = 100,
) -> str:
    """Supplier invoices (MIRO) via API_SUPPLIERINVOICE_PROCESS_SRV / A_SupplierInvoice. Read-only.

    For invoices *posted but blocked* for payment, read with
    ``payment_blocked_only=True`` (``PaymentBlockingReason`` not blank). A GR
    with no invoice at all is the absence of a matching row here — detect it by
    correlating ``material_documents`` (GR posted) against this, i.e. a join.

    Args:
        company_code: SAP company code.
        invoicing_party: Supplier number (``InvoicingParty``).
        fiscal_year: Invoice ``FiscalYear`` (with ``SupplierInvoice`` forms
            the key).
        posting_date_from / posting_date_to: ``YYYY-MM-DD`` on ``PostingDate``.
        payment_blocked_only: If True, keep only invoices with a non-blank
            ``PaymentBlockingReason``.
        supplier_invoice: A single invoice number.
        include_po_items: Expand ``to_SuplrInvcItemPurOrdRef`` (the PO-
            reference items that link the invoice back to its PO/GR).
        select / filter: Optional raw OData ``$select`` / extra ``$filter``.
        top: Max headers (default 100).
    """
    f = []
    if company_code:
        f.append(f"CompanyCode eq '{company_code}'")
    if invoicing_party:
        f.append(f"InvoicingParty eq '{invoicing_party}'")
    if fiscal_year:
        f.append(f"FiscalYear eq '{fiscal_year}'")
    if posting_date_from:
        f.append(f"PostingDate ge datetime'{posting_date_from}T00:00:00'")
    if posting_date_to:
        f.append(f"PostingDate le datetime'{posting_date_to}T23:59:59'")
    if payment_blocked_only:
        f.append("PaymentBlockingReason ne ''")
    if supplier_invoice:
        f.append(f"SupplierInvoice eq '{supplier_invoice}'")
    if filter:
        f.append(f"({filter})")
    return _safe(lambda: _get_client().query(
        _SINV_SERVICE, _SINV_HEADER_ENTITY,
        select=select,
        filter=" and ".join(f) if f else None,
        orderby="PostingDate desc",
        expand="to_SuplrInvcItemPurOrdRef" if include_po_items else None,
        top=top,
    ))


# AP open-items aggregator -----------------------------------------------------
# Built over the same universal-journal item cube the GL tools use. The fields
# beyond the GL set (Supplier, NetDueDate, FinancialAccountType, and the
# clearing/open indicator) are documented on the cube but should be confirmed
# in-tenant; if the cube does not cleanly carry open-item status, switch the
# source to a dedicated vendor-open-items service. The aggregation is
# source-agnostic.
_AP_SERVICE = "API_OPLACCTGDOCITEMCUBE_SRV"
_AP_ENTITY = "A_OperationalAcctgDocItemCube"
_AP_SELECT = (
    "CompanyCode,Supplier,GLAccount,FinancialAccountType,"
    "AmountInCompanyCodeCurrency,CompanyCodeCurrency,DebitCreditCode,"
    "PostingDate,NetDueDate,ClearingAccountingDocument,ClearingDate"
)


@mcp.tool()
def ap_open_items(
    company_code: str,
    as_of_date: Optional[str] = None,
    supplier: Optional[str] = None,
    horizon_days: int = 15,
    max_lines: int = 200_000,
) -> str:
    """Open accounts-payable items, bucketed by net due date. Read-only.

    Returns open vendor payables grouped by supplier and a due-date bucket, so
    Finance sees what is due in the next 10/15 days ahead of time.

    Buckets (relative to ``as_of_date``, default today): ``overdue`` (due
    date already passed), ``due_0_10`` (0–10 days), ``due_11_15`` (11–15),
    and ``beyond`` (after the horizon).

    In-tenant confirmation: the open-item predicate here is
    "``ClearingAccountingDocument`` is blank" and vendor items are
    ``FinancialAccountType eq 'K'``. Confirm both field names against the
    tenant; if the cube does not expose clearing status cleanly, point this
    tool at a dedicated vendor-open-items service — the bucketing is unchanged.

    Args:
        company_code: SAP company code (required).
        as_of_date: ``YYYY-MM-DD`` anchor for the buckets (default: today).
        supplier: Optional single supplier filter.
        horizon_days: The far edge of the planning window (default 15).
        max_lines: Safety cap on items scanned (default 200k).
    """
    def _do():
        client = _get_client()
        as_of = as_of_date or _dt.date.today().isoformat()
        as_of_d = _dt.date.fromisoformat(as_of)
        f = [
            f"CompanyCode eq '{company_code}'",
            "FinancialAccountType eq 'K'",
            "ClearingAccountingDocument eq ''",
        ]
        if supplier:
            f.append(f"Supplier eq '{supplier}'")

        sups: dict[str, dict] = {}
        line_count = 0
        currency = ""
        totals = {"overdue": 0.0, "due_0_10": 0.0, "due_11_15": 0.0, "beyond": 0.0, "no_due_date": 0.0}

        def _bucket(due: Optional[str]) -> str:
            if not due:
                return "no_due_date"
            try:
                # SAP OData v2 dates arrive as /Date(ms)/ or ISO; tolerate both.
                if due.startswith("/Date("):
                    ms = int(due[6:].split(")")[0].split("+")[0])
                    d = _dt.date(1970, 1, 1) + _dt.timedelta(milliseconds=ms)
                else:
                    d = _dt.date.fromisoformat(due[:10])
            except (ValueError, TypeError):
                return "no_due_date"
            delta = (d - as_of_d).days
            if delta < 0:
                return "overdue"
            if delta <= 10:
                return "due_0_10"
            if delta <= horizon_days:
                return "due_11_15"
            return "beyond"

        for row in client.query_all(
            _AP_SERVICE, _AP_ENTITY,
            select=_AP_SELECT, filter=" and ".join(f),
            orderby="NetDueDate asc",
            page_size=1000, max_records=max_lines,
        ):
            sup = row.get("Supplier") or "(unknown)"
            amt = _to_float(row.get("AmountInCompanyCodeCurrency"))
            bucket = _bucket(row.get("NetDueDate"))
            entry = sups.setdefault(sup, {
                "supplier": sup,
                "open_amount": 0.0,
                "overdue": 0.0, "due_0_10": 0.0, "due_11_15": 0.0,
                "beyond": 0.0, "no_due_date": 0.0,
                "item_count": 0,
            })
            entry["open_amount"] += amt
            entry[bucket] += amt
            entry["item_count"] += 1
            totals[bucket] += amt
            line_count += 1
            if not currency:
                currency = row.get("CompanyCodeCurrency", "") or ""

        rows = []
        for e in sups.values():
            for k in ("open_amount", "overdue", "due_0_10", "due_11_15", "beyond", "no_due_date"):
                e[k] = round(e[k], 2)
            rows.append(e)
        rows.sort(key=lambda r: -abs(r["open_amount"]))
        return {
            "company_code": company_code,
            "as_of_date": as_of,
            "currency": currency,
            "horizon_days": horizon_days,
            "supplier_count": len(rows),
            "scanned_line_count": line_count,
            "truncated": line_count >= max_lines,
            "totals_by_bucket": {k: round(v, 2) for k, v in totals.items()},
            "open_total": round(sum(totals.values()), 2),
            "rows": rows,
            "source_entity": f"{_AP_SERVICE}/{_AP_ENTITY}",
            "note": "Vendor open items; open = blank ClearingAccountingDocument, vendor = FinancialAccountType 'K'. Confirm both in-tenant.",
        }
    return _safe(_do)


# ===========================================================================
# SD (sales & distribution / order-to-cash) read tools. Strictly read-only.
#
# Dedicated wrappers over the released OData V2 sales services. Billing is
# already covered by ``sales_by_material`` over API_BILLING_DOCUMENT_SRV, so the
# three here are the sales order, the outbound delivery, and AR open items (the
# customer-side mirror of ``ap_open_items``). As with the P2P tools, readers
# avoid a curated $select and expose select / filter / expand passthroughs so
# "confirm in-tenant" fields can be pinned without code changes.
#
#   Service                      Header / item entities (V2)
#   API_SALES_ORDER_SRV          A_SalesOrder / A_SalesOrderItem / A_SalesOrderScheduleLine
#   API_OUTBOUND_DELIVERY_SRV    A_OutbDeliveryHeader / A_OutbDeliveryItem
#   API_OPLACCTGDOCITEMCUBE_SRV  A_OperationalAcctgDocItemCube (customer open items)
# ===========================================================================

_SO_SERVICE = "API_SALES_ORDER_SRV"
_SO_HEADER_ENTITY = "A_SalesOrder"


@mcp.tool()
def sales_orders(
    sales_organization: Optional[str] = None,
    distribution_channel: Optional[str] = None,
    sold_to_party: Optional[str] = None,
    sales_order: Optional[str] = None,
    sales_order_type: Optional[str] = None,
    so_date_from: Optional[str] = None,
    so_date_to: Optional[str] = None,
    include_items: bool = False,
    select: Optional[str] = None,
    filter: Optional[str] = None,  # noqa: A002 — extra raw OData $filter, AND-ed in
    top: int = 100,
) -> str:
    """Sales orders via API_SALES_ORDER_SRV / A_SalesOrder. Read-only.

    Returns sales-order headers. Pass ``include_items=True`` to expand
    ``to_Item`` (item: Material, SalesOrderItem, RequestedQuantity, NetAmount,
    and the delivery-status fields).

    Delivery-date caveat: the requested / confirmed delivery date is on the
    **schedule line**, not the header — it lives on
    ``A_SalesOrderScheduleLine`` (``RequestedDeliveryDate`` /
    ``ConfirmedDeliveryDate``), reached via ``to_Item/to_ScheduleLine``. Some
    releases also carry an order-level ``RequestedDeliveryDate`` on the header;
    read the schedule line via ``query_entity`` when item-level dates are needed.

    Funnel caveat: inquiries and quotations are separate sales document
    categories. Depending on the tenant they are reachable here via
    ``SalesOrderType``, or through the dedicated ``API_SALES_INQUIRY_SRV`` /
    ``API_SALES_QUOTATION_SRV``. Pass inquiry / quotation document types through
    ``sales_order_type`` / ``filter`` if the tenant projects them here.

    Args:
        sales_organization / distribution_channel: SAP SD org units.
        sold_to_party: Customer number.
        sales_order: A single sales-order number.
        sales_order_type: Document type (order ``OR``, quotation ``QT``,
            inquiry ``IN`` — confirm the tenant's type codes).
        so_date_from / so_date_to: ``YYYY-MM-DD`` on ``SalesOrderDate``.
        include_items: Expand ``to_Item`` when True.
        select / filter: Optional raw OData ``$select`` / extra ``$filter``.
        top: Max headers (default 100).
    """
    f = []
    if sales_organization:
        f.append(f"SalesOrganization eq '{sales_organization}'")
    if distribution_channel:
        f.append(f"DistributionChannel eq '{distribution_channel}'")
    if sold_to_party:
        f.append(f"SoldToParty eq '{sold_to_party}'")
    if sales_order:
        f.append(f"SalesOrder eq '{sales_order}'")
    if sales_order_type:
        f.append(f"SalesOrderType eq '{sales_order_type}'")
    if so_date_from:
        f.append(f"SalesOrderDate ge datetime'{so_date_from}T00:00:00'")
    if so_date_to:
        f.append(f"SalesOrderDate le datetime'{so_date_to}T23:59:59'")
    if filter:
        f.append(f"({filter})")
    return _safe(lambda: _get_client().query(
        _SO_SERVICE, _SO_HEADER_ENTITY,
        select=select,
        filter=" and ".join(f) if f else None,
        orderby="SalesOrderDate desc",
        expand="to_Item" if include_items else None,
        top=top,
    ))


_OBD_SERVICE = "API_OUTBOUND_DELIVERY_SRV;v=2"  # v1 has no system alias on this tenant; v2 is the active version (confirmed live 16 Jun)
_OBD_HEADER_ENTITY = "A_OutbDeliveryHeader"


@mcp.tool()
def outbound_deliveries(
    shipping_point: Optional[str] = None,
    sales_organization: Optional[str] = None,
    sold_to_party: Optional[str] = None,
    ship_to_party: Optional[str] = None,
    delivery_document: Optional[str] = None,
    planned_gi_date_from: Optional[str] = None,
    planned_gi_date_to: Optional[str] = None,
    not_yet_goods_issued: bool = False,
    include_items: bool = False,
    select: Optional[str] = None,
    filter: Optional[str] = None,  # noqa: A002
    top: int = 100,
) -> str:
    """Outbound deliveries / dispatch via API_OUTBOUND_DELIVERY_SRV. Read-only.

    Returns delivery headers. Dispatch is anchored on the dates the header
    carries: ``PlannedGoodsIssueDate`` (the planned dispatch day) and
    ``ActualGoodsMovementDate`` (when goods were actually issued — blank until
    dispatch). Pass ``not_yet_goods_issued=True`` to keep only deliveries with
    no ``ActualGoodsMovementDate`` (planned but not yet dispatched).

    POD caveat: the header carries a proof-of-delivery **status** field
    (``ProofOfDeliveryStatus``), but the actual proof / acknowledgement document
    is typically captured off-system.

    Args:
        shipping_point: Shipping point.
        sales_organization: SAP sales organization.
        sold_to_party / ship_to_party: Customer / ship-to numbers.
        delivery_document: A single outbound-delivery number.
        planned_gi_date_from / planned_gi_date_to: ``YYYY-MM-DD`` on
            ``PlannedGoodsIssueDate``.
        not_yet_goods_issued: If True, keep only deliveries with a blank
            ``ActualGoodsMovementDate`` (dispatch not yet posted). The null-date
            predicate should be confirmed in-tenant.
        include_items: Expand ``to_DeliveryDocumentItem`` when True.
        select / filter: Optional raw OData ``$select`` / extra ``$filter``.
        top: Max headers (default 100).
    """
    f = []
    if shipping_point:
        f.append(f"ShippingPoint eq '{shipping_point}'")
    if sales_organization:
        f.append(f"SalesOrganization eq '{sales_organization}'")
    if sold_to_party:
        f.append(f"SoldToParty eq '{sold_to_party}'")
    if ship_to_party:
        f.append(f"ShipToParty eq '{ship_to_party}'")
    if delivery_document:
        f.append(f"DeliveryDocument eq '{delivery_document}'")
    if planned_gi_date_from:
        f.append(f"PlannedGoodsIssueDate ge datetime'{planned_gi_date_from}T00:00:00'")
    if planned_gi_date_to:
        f.append(f"PlannedGoodsIssueDate le datetime'{planned_gi_date_to}T23:59:59'")
    if not_yet_goods_issued:
        # ActualGoodsMovementDate is blank until goods issue (dispatch) is
        # posted. The exact null predicate may differ by tenant (eq null vs a
        # sentinel date) — confirm in-tenant.
        f.append("ActualGoodsMovementDate eq null")
    if filter:
        f.append(f"({filter})")
    return _safe(lambda: _get_client().query(
        _OBD_SERVICE, _OBD_HEADER_ENTITY,
        select=select,
        filter=" and ".join(f) if f else None,
        orderby="PlannedGoodsIssueDate desc",
        expand="to_DeliveryDocumentItem" if include_items else None,
        top=top,
    ))


# AR open-items aggregator -----------------------------------------------------
# The customer-side mirror of ap_open_items, over the same universal-journal
# item cube. Customer open items are FinancialAccountType 'D' (Debitor); the
# open predicate is a blank ClearingAccountingDocument, same as the AP side.
# Confirm both in-tenant; if the cube does not expose customer open-item status
# cleanly, point this at a dedicated customer-open-items service — the bucketing
# is source-agnostic.
_AR_SERVICE = "API_OPLACCTGDOCITEMCUBE_SRV"
_AR_ENTITY = "A_OperationalAcctgDocItemCube"
_AR_SELECT = (
    "CompanyCode,Customer,GLAccount,FinancialAccountType,"
    "AmountInCompanyCodeCurrency,CompanyCodeCurrency,DebitCreditCode,"
    "PostingDate,NetDueDate,ClearingAccountingDocument,ClearingDate"
)


@mcp.tool()
def ar_open_items(
    company_code: str,
    as_of_date: Optional[str] = None,
    customer: Optional[str] = None,
    horizon_days: int = 30,
    max_lines: int = 200_000,
) -> str:
    """Open accounts-receivable items, bucketed by net due date. Read-only.

    The customer-side mirror of ``ap_open_items``. Returns open customer
    receivables grouped by customer and a due-date bucket, so sales and finance
    see who owes, how many invoices, and how much — both the forward "who has to
    pay next month" view and the overdue ageing that drives late-payer follow-up.

    Buckets (relative to ``as_of_date``, default today), by net due date:
    ``overdue_90_plus``, ``overdue_61_90``, ``overdue_31_60``, ``overdue_1_30``
    (already past due), ``due_0_30`` (coming due within the horizon), ``beyond``
    (due after the horizon), and ``no_due_date``. Each row also carries an
    ``overdue_total`` convenience sum.

    In-tenant confirmation: the open-item predicate here is
    "``ClearingAccountingDocument`` is blank" and customer items are
    ``FinancialAccountType eq 'D'``. Confirm both field names against the
    tenant; if the cube does not expose clearing status cleanly, point this
    tool at a dedicated customer-open-items service — the bucketing is unchanged.

    Args:
        company_code: SAP company code (required).
        as_of_date: ``YYYY-MM-DD`` anchor for the buckets (default: today).
        customer: Optional single customer filter.
        horizon_days: The far edge of the "coming due" window (default 30).
        max_lines: Safety cap on items scanned (default 200k).
    """
    def _do():
        client = _get_client()
        as_of = as_of_date or _dt.date.today().isoformat()
        as_of_d = _dt.date.fromisoformat(as_of)
        f = [
            f"CompanyCode eq '{company_code}'",
            "FinancialAccountType eq 'D'",
            "ClearingAccountingDocument eq ''",
        ]
        if customer:
            f.append(f"Customer eq '{customer}'")

        bucket_keys = ("overdue_90_plus", "overdue_61_90", "overdue_31_60",
                       "overdue_1_30", "due_0_30", "beyond", "no_due_date")
        custs: dict[str, dict] = {}
        line_count = 0
        currency = ""
        totals = {k: 0.0 for k in bucket_keys}

        def _bucket(due: Optional[str]) -> str:
            if not due:
                return "no_due_date"
            try:
                if due.startswith("/Date("):
                    ms = int(due[6:].split(")")[0].split("+")[0])
                    d = _dt.date(1970, 1, 1) + _dt.timedelta(milliseconds=ms)
                else:
                    d = _dt.date.fromisoformat(due[:10])
            except (ValueError, TypeError):
                return "no_due_date"
            delta = (d - as_of_d).days   # negative = overdue
            if delta < -90:
                return "overdue_90_plus"
            if delta <= -61:
                return "overdue_61_90"
            if delta <= -31:
                return "overdue_31_60"
            if delta < 0:
                return "overdue_1_30"
            if delta <= horizon_days:
                return "due_0_30"
            return "beyond"

        for row in client.query_all(
            _AR_SERVICE, _AR_ENTITY,
            select=_AR_SELECT, filter=" and ".join(f),
            orderby="NetDueDate asc",
            page_size=1000, max_records=max_lines,
        ):
            cust = row.get("Customer") or "(unknown)"
            amt = _to_float(row.get("AmountInCompanyCodeCurrency"))
            bucket = _bucket(row.get("NetDueDate"))
            entry = custs.setdefault(cust, {
                "customer": cust,
                "open_amount": 0.0,
                "invoice_count": 0,
                **{k: 0.0 for k in bucket_keys},
            })
            entry["open_amount"] += amt
            entry[bucket] += amt
            entry["invoice_count"] += 1
            totals[bucket] += amt
            line_count += 1
            if not currency:
                currency = row.get("CompanyCodeCurrency", "") or ""

        rows = []
        for e in custs.values():
            for k in ("open_amount", *bucket_keys):
                e[k] = round(e[k], 2)
            e["overdue_total"] = round(
                e["overdue_1_30"] + e["overdue_31_60"]
                + e["overdue_61_90"] + e["overdue_90_plus"], 2)
            rows.append(e)
        rows.sort(key=lambda r: -abs(r["open_amount"]))
        overdue_total = round(
            totals["overdue_1_30"] + totals["overdue_31_60"]
            + totals["overdue_61_90"] + totals["overdue_90_plus"], 2)
        return {
            "company_code": company_code,
            "as_of_date": as_of,
            "currency": currency,
            "horizon_days": horizon_days,
            "customer_count": len(rows),
            "scanned_line_count": line_count,
            "truncated": line_count >= max_lines,
            "totals_by_bucket": {k: round(v, 2) for k, v in totals.items()},
            "overdue_total": overdue_total,
            "open_total": round(sum(totals.values()), 2),
            "rows": rows,
            "source_entity": f"{_AR_SERVICE}/{_AR_ENTITY}",
            "note": "Customer open items; open = blank ClearingAccountingDocument, customer = FinancialAccountType 'D'. Confirm both in-tenant.",
        }
    return _safe(_do)



# ===========================================================================
# Internal / plant-operations read tools. Strictly read-only.
#
# Wrappers over the two plant-health inputs that live in SAP: material on-hand
# stock (raw material and spares, for the below-reorder watch) and SAP PM
# maintenance orders (for the maintenance-schedule watch). Each reader avoids a
# curated $select, tolerates missing fields, and exposes a raw filter
# passthrough, so "confirm in-tenant" fields can be pinned without code changes.
#
#   Service                  Entity                     Use
#   API_MATERIAL_STOCK_SRV   A_MatlStkInAcctMod         on-hand stock (raw material, spares)
#   API_PRODUCT_SRV          A_ProductPlant             reorder point (ReorderThresholdQuantity)
#   API_MAINTENANCEORDER     MaintenanceOrder           maintenance-schedule adherence
# ===========================================================================

_STOCK_SERVICE = "API_MATERIAL_STOCK_SRV"
_STOCK_ENTITY = "A_MatlStkInAcctMod"  # A_MaterialStock is a header stub (no Plant/qty); on-hand qty + Plant live on A_MatlStkInAcctMod (confirmed live 16 Jun)
_PRODPLANT_SERVICE = "API_PRODUCT_SRV"
_PRODPLANT_ENTITY = "A_ProductPlant"


@mcp.tool()
def material_stock(
    plant: Optional[str] = None,
    material: Optional[str] = None,
    only_below_reorder: bool = False,
    check_reorder: bool = True,
    filter: Optional[str] = None,  # noqa: A002 — extra raw OData $filter, AND-ed in
    max_lines: int = 200_000,
) -> str:
    """On-hand material stock vs reorder point. Read-only.

    Sums warehouse on-hand quantity per material and plant from
    API_MATERIAL_STOCK_SRV / A_MatlStkInAcctMod, then (when ``check_reorder`` is
    True) reads the reorder point from API_PRODUCT_SRV / A_ProductPlant
    (``ReorderThresholdQuantity``) and flags any material whose on-hand stock
    has fallen below it, with the shortfall.

    Raw material vs spares is not a field on the stock row; scope it by material
    type or material group (raw material ``ROH`` / spares ``ERSA``), or pass a
    ``material`` / ``filter`` predicate.

    In-tenant confirmation: the on-hand quantity field is assumed to be
    ``MatlWrhsStkQtyInMatlBaseUnit`` and the reorder field
    ``ReorderThresholdQuantity`` on ``A_ProductPlant``. Confirm both names and
    the stock service/version against the tenant; if the stock entity differs,
    the aggregation is unchanged — only the field names move.

    Args:
        plant: SAP plant, e.g. ``"1000"``.
        material: A single material/product number.
        only_below_reorder: Return only the materials below their reorder point.
        check_reorder: Read and join the reorder point (default True). If the
            MRP read fails or is not licensed, on-hand stock is still returned.
        filter: Optional raw OData ``$filter`` AND-ed onto the stock query.
        max_lines: Safety cap on stock rows scanned (default 200k).
    """
    def _do():
        client = _get_client()
        f = []
        if plant:
            f.append(f"Plant eq '{plant}'")
        if material:
            f.append(f"Material eq '{material}'")
        if filter:
            f.append(f"({filter})")

        mats: dict[tuple, dict] = {}
        line_count = 0
        for row in client.query_all(
            _STOCK_SERVICE, _STOCK_ENTITY,
            filter=" and ".join(f) if f else None,
            page_size=1000, max_records=max_lines,
        ):
            mat = row.get("Material") or "(unknown)"
            plt = row.get("Plant") or ""
            qty = _to_float(
                row.get("MatlWrhsStkQtyInMatlBaseUnit")
                or row.get("MaterialBaseUnitStockQuantity")
                or row.get("Quantity")
            )
            unit = row.get("MaterialBaseUnit") or row.get("BaseUnit") or ""
            key = (mat, plt)
            e = mats.setdefault(key, {
                "material": mat, "plant": plt, "base_unit": unit, "on_hand_qty": 0.0,
            })
            e["on_hand_qty"] += qty
            if unit and not e["base_unit"]:
                e["base_unit"] = unit
            line_count += 1

        reorder: dict[tuple, float] = {}
        if check_reorder:
            rf = []
            if plant:
                rf.append(f"Plant eq '{plant}'")
            if material:
                rf.append(f"Material eq '{material}'")
            try:
                for row in client.query_all(
                    _PRODPLANT_SERVICE, _PRODPLANT_ENTITY,
                    filter=" and ".join(rf) if rf else None,
                    page_size=1000, max_records=max_lines,
                ):
                    mat = row.get("Material")
                    plt = row.get("Plant") or ""
                    rp = row.get("ReorderThresholdQuantity")
                    if mat is not None and rp not in (None, ""):
                        reorder[(mat, plt)] = _to_float(rp)
            except SapODataError:
                reorder = {}  # MRP read is optional; on-hand still returned

        rows = []
        below = 0
        for key, e in mats.items():
            e["on_hand_qty"] = round(e["on_hand_qty"], 3)
            rp = reorder.get(key)
            if rp is not None:
                e["reorder_point"] = round(rp, 3)
                e["below_reorder"] = e["on_hand_qty"] < rp
                e["shortfall"] = round(max(0.0, rp - e["on_hand_qty"]), 3)
                if e["below_reorder"]:
                    below += 1
            rows.append(e)
        if only_below_reorder:
            rows = [r for r in rows if r.get("below_reorder")]
        rows.sort(key=lambda r: (-(r.get("shortfall") or 0.0), str(r["material"])))
        return {
            "plant": plant,
            "material": material,
            "material_count": len(mats),
            "scanned_line_count": line_count,
            "below_reorder_count": below,
            "reorder_checked": bool(check_reorder and reorder),
            "rows": rows,
            "source_entity": f"{_STOCK_SERVICE}/{_STOCK_ENTITY}",
            "note": (
                "On-hand stock summed per material/plant. Reorder point read from "
                "API_PRODUCT_SRV/A_ProductPlant.ReorderThresholdQuantity; below_reorder = "
                "on_hand < reorder. Confirm the stock quantity field "
                "(MatlWrhsStkQtyInMatlBaseUnit) and the reorder field in-tenant."
            ),
        }
    return _safe(_do)


_PM_SERVICE = "API_MAINTENANCEORDER"
_PM_ENTITY = "MaintenanceOrder"
_PM_DONE_PHASES = {"TECO", "CLSD", "CLOSED", "COMPLETED", "COMPLETE"}


@mcp.tool()
def maintenance_orders(
    plant: Optional[str] = None,
    functional_location: Optional[str] = None,
    order_type: Optional[str] = None,
    as_of_date: Optional[str] = None,
    horizon_days: int = 14,
    only_open: bool = True,
    filter: Optional[str] = None,  # noqa: A002 — extra raw OData $filter, AND-ed in
    max_lines: int = 100_000,
) -> str:
    """SAP PM maintenance orders, bucketed against their schedule. Read-only.

    Reads API_MAINTENANCEORDER / MaintenanceOrder and buckets each open order
    by its basic end date relative to ``as_of_date``: ``overdue`` (the
    scheduled date has passed and the order is not yet completed), ``due_soon``
    (due within ``horizon_days``), and ``scheduled`` (beyond the horizon).

    In-tenant confirmation: the basic-date field is assumed
    ``MaintenanceOrderBasicEndDate``, the completion signal a phase field
    (``MaintenanceProcessingPhase``), the planning plant
    ``MaintenancePlanningPlant``, and the object ``FunctionalLocation``. Confirm
    these and the OData version against the tenant; if SAP PM is not in use,
    maintenance scheduling may be off-system rather than read here.

    Args:
        plant: Maintenance planning plant, e.g. ``"1000"``.
        functional_location: A single functional location / technical object.
        order_type: Maintenance order type (e.g. preventive ``PM01``).
        as_of_date: ``YYYY-MM-DD`` anchor for the buckets (default: today).
        horizon_days: The forward window for ``due_soon`` (default 14).
        only_open: Exclude technically-completed / closed orders (default True).
        filter: Optional raw OData ``$filter`` AND-ed onto the typed filters.
        max_lines: Safety cap on orders scanned (default 100k).
    """
    def _do():
        client = _get_client()
        as_of = as_of_date or _dt.date.today().isoformat()
        as_of_d = _dt.date.fromisoformat(as_of)
        f = []
        if plant:
            f.append(f"MaintenancePlanningPlant eq '{plant}'")
        if functional_location:
            f.append(f"FunctionalLocation eq '{functional_location}'")
        if order_type:
            f.append(f"MaintenanceOrderType eq '{order_type}'")
        if filter:
            f.append(f"({filter})")

        def _pd(v):
            if not v:
                return None
            s = str(v)
            try:
                if s.startswith("/Date("):
                    ms = int(s[6:].split(")")[0].split("+")[0])
                    return _dt.date(1970, 1, 1) + _dt.timedelta(milliseconds=ms)
                return _dt.date.fromisoformat(s[:10])
            except (ValueError, TypeError):
                return None

        totals = {"overdue": 0, "due_soon": 0, "scheduled": 0, "no_date": 0}
        rows = []
        line_count = 0
        for row in client.query_all(
            _PM_SERVICE, _PM_ENTITY,
            filter=" and ".join(f) if f else None,
            orderby="MaintenanceOrder asc",
            page_size=1000, max_records=max_lines,
        ):
            phase = str(row.get("MaintenanceProcessingPhase")
                        or row.get("OrderStatus") or "").upper()
            if only_open and phase in _PM_DONE_PHASES:
                continue
            end = _pd(row.get("MaintenanceOrderBasicEndDate")
                      or row.get("MaintenanceOrderScheduledEndDate"))
            if end is None:
                bucket = "no_date"
            else:
                delta = (end - as_of_d).days
                bucket = "overdue" if delta < 0 else ("due_soon" if delta <= horizon_days else "scheduled")
            totals[bucket] += 1
            rows.append({
                "maintenance_order": row.get("MaintenanceOrder"),
                "order_type": row.get("MaintenanceOrderType"),
                "functional_location": row.get("FunctionalLocation") or row.get("TechnicalObject"),
                "basic_start": row.get("MaintenanceOrderBasicStartDate"),
                "basic_end": row.get("MaintenanceOrderBasicEndDate"),
                "bucket": bucket,
            })
            line_count += 1

        rank = {"overdue": 0, "due_soon": 1, "no_date": 2, "scheduled": 3}
        rows.sort(key=lambda r: rank.get(r["bucket"], 9))
        return {
            "as_of_date": as_of,
            "horizon_days": horizon_days,
            "order_count": line_count,
            "totals_by_bucket": totals,
            "rows": rows,
            "source_entity": f"{_PM_SERVICE}/{_PM_ENTITY}",
            "note": (
                "SAP PM maintenance orders bucketed by basic end date vs as_of "
                "(overdue / due within horizon / scheduled). Assumes SAP PM is in use; "
                "if not, maintenance scheduling may be off-system. Confirm field names in-tenant."
            ),
        }
    return _safe(_do)


# ---------------------------------------------------------------------------
# SAP tables: the first choice for reading anything.
#
# Backed by the custom ZCFG_READ_SRV service (see sap_side/README.md). SAP
# checks S_TABU_NAM per table under the caller's own user, so these tools see
# exactly what that user could see in SE16.
# ---------------------------------------------------------------------------


@mcp.tool()
def read_sap_table(
    table: str,
    where: str = "",
    fields: str = "",
    top: int = 100,
    skip: int = 0,
    drop_empty: bool = True,
) -> str:
    """Read rows of any SAP table or database view, like SE16. The FIRST CHOICE
    for reading SAP data: master data, documents, line items and configuration.

    For totals use aggregate_sap_table instead of paging rows. Call
    describe_sap_table first if you are not sure of the field names. For
    pricing and account determination, the shortcuts condition_types,
    pricing_procedure and account_determination add texts.

    Args:
        table: Table or database view name, e.g. ``"ACDOCA"``, ``"EKKO"``, ``"T001"``.
        where: ABAP Open SQL condition — NOT OData. Literals in single
            quotes, internal formats (leading zeros, dates as
            ``'20250401'``), e.g. ``"KAPPL = 'M' AND KSCHL LIKE 'Z%'"``.
            Empty reads the whole table (up to ``top``).
        fields: Comma-separated field list. Key fields are always included.
            Empty returns every field — wide tables then get large, so name
            the fields you need.
        top: Rows to return (max 1000). ``more_available`` / ``next_skip``
            in the answer tell you when to page.
        skip: Rows to skip, for paging (rows come in primary-key order).
        drop_empty: Omit blank fields from each row (default True).
    """
    return _safe(lambda: config_tables.read_table(
        _get_client(), table, where=where, fields=fields,
        max_rows=top, skip=skip, drop_empty=drop_empty,
    ))


@mcp.tool()
def aggregate_sap_table(
    table: str,
    measures: str = "COUNT",
    group_by: str = "",
    where: str = "",
    order_by: str = "",
    top: int = 500,
) -> str:
    """Totals computed inside SAP (SUM / COUNT / MIN / MAX / AVG with GROUP BY).

    The fast way to get balances, totals and counts from big tables. One call
    replaces paging through thousands of rows, e.g. a trial balance:
    ``table="ACDOCA", measures="SUM:HSL",
    group_by="RACCT", where="RLDNR = '0L' AND RBUKRS = '1000' AND GJAHR = '2026'"``.

    Args:
        table: Table or database view, e.g. ``"ACDOCA"``, ``"VBRP"``, ``"MATDOC"``.
        measures: Comma-separated: ``SUM:field``, ``AVG:field``, ``MIN:field``,
            ``MAX:field``, ``COUNT`` (rows), ``COUNTD:field`` (distinct values).
            Results appear as ``SUM_HSL``, ``ROW_COUNT``, ``COUNTD_BELNR`` and so on.
        group_by: Comma-separated fields to group by. Empty = one total row.
        where: ABAP Open SQL condition (same rules as read_sap_table).
        order_by: Sort by result columns, e.g. ``"SUM_HSL DESC"`` with ``top=20``
            for a top-20 list. Default: by the group-by fields.
        top: Maximum groups to return (max 1000).
    """
    return _safe(lambda: compute.aggregate(
        _get_client(), table, measures=measures, group_by=group_by,
        where=where, order_by=order_by, top=top,
    ))


@mcp.tool()
def find_duplicates(table: str, fields: str, where: str = "", min_count: int = 2, top: int = 200) -> str:
    """Groups of rows that share the same values on the given fields, computed
    in SAP, most frequent first. E.g. possible duplicate supplier invoices:
    ``table="RBKP", fields="LIFNR,XBLNR,RMWWR", where="BUKRS = '1000' AND GJAHR = '2026'"``.

    Args:
        table: Table or database view.
        fields: Comma-separated fields that together should be unique.
        where: ABAP Open SQL condition.
        min_count: Smallest group size to report (default 2).
        top: Maximum groups to return (max 1000).
    """
    return _safe(lambda: compute.duplicates(
        _get_client(), table, fields, where=where, min_count=min_count, top=top))


@mcp.tool()
def trial_balance_by_period(
    company_code: str,
    fiscal_year: str,
    period_from: int = 1,
    period_to: int = 16,
    account_from: str = "",
    account_to: str = "",
    ledger: str = "0L",
) -> str:
    """Trial balance computed in SAP from ACDOCA: per G/L account the opening
    balance (incl. carry-forward), period debit, period credit and closing
    balance, with account names. One call and fast; prefer this over
    trial_balance unless you need posting-date (not period) cut-offs.

    Args:
        company_code: e.g. ``"1000"``.
        fiscal_year: e.g. ``"2026"``.
        period_from / period_to: Posting periods (1-16). Opening = all
            periods before period_from.
        account_from / account_to: Optional G/L account range.
        ledger: Default ``"0L"`` (leading ledger).
    """
    return _safe(lambda: compute.trial_balance(
        _get_client(), company_code, fiscal_year, period_from=period_from, period_to=period_to,
        account_from=account_from, account_to=account_to, ledger=ledger))


@mcp.tool()
def open_items_ageing(
    account_type: str,
    company_code: str,
    key_date: str = "",
    partner: str = "",
    top: int = 500,
) -> str:
    """Vendor or customer ageing computed in SAP: open items at a key date per
    partner, in buckets by days past net due date (not due, 1-30, 31-60,
    61-90, 91-180, over 180), with names and totals.

    Args:
        account_type: ``"vendor"`` or ``"customer"``.
        company_code: e.g. ``"1000"``.
        key_date: ``YYYY-MM-DD``; items open on that date. Default today.
        partner: Optional single vendor / customer number.
        top: Maximum partners (max 1000).
    """
    return _safe(lambda: compute.ageing(
        _get_client(), account_type, company_code, key_date=key_date, partner=partner, top=top))


@mcp.tool()
def grir_differences(
    company_code: str,
    vendor: str = "",
    po: str = "",
    key_date: str = "",
    top: int = 500,
) -> str:
    """GR/IR differences computed in SAP: per PO item, goods-receipt vs
    invoice-receipt quantity and value (EKBE), only items that do not match.
    Use for GR/IR clearing account analysis.

    Args:
        company_code: e.g. ``"1000"``.
        vendor: Optional vendor number.
        po: Optional purchase order number.
        key_date: ``YYYY-MM-DD``; history posted up to then. Default today.
        top: Maximum PO items (max 1000).
    """
    return _safe(lambda: compute.grir(
        _get_client(), company_code, vendor=vendor, po=po, key_date=key_date, top=top))


@mcp.tool()
def describe_sap_table(table: str, search: str = "", refresh: bool = False) -> str:
    """List a table's fields (name, type, length, key flag, description).

    Cached for 7 days after the first call. Use ``search`` to find a field
    by name or description without listing all of them.

    Args:
        table: Table name, e.g. ``"T030"``.
        search: Optional case-insensitive filter on field name / description.
        refresh: Ignore the cache and re-read the field list from SAP.
    """
    return _safe(lambda: config_tables.describe_table(_get_client(), table, search, refresh))


@mcp.tool()
def condition_types(
    application: str = "V",
    condition_type: str = "",
    search: str = "",
    usage: str = "A",
    language: str = "E",
    limit: int = 300,
    offset: int = 0,
) -> str:
    """Condition types with their texts, access sequence and control data.

    Reads T685 / T685T / T685A. Without ``condition_type`` you get a table
    (``columns`` + ``rows``) with the main control fields (calculation type
    KRECH, condition class KOAID, ...), paged ``limit`` at a time; with one
    you get every filled control field. A ``field_legend`` explains the
    field names. Sales (V) has around a thousand condition types, so prefer
    ``search`` over paging.

    Args:
        application: ``"V"`` sales/distribution, ``"M"`` purchasing,
            ``"TX"`` taxes, etc.
        condition_type: Optional single condition type, e.g. ``"PB00"``.
        search: Optional text filter on condition type or description.
        usage: Condition usage, ``"A"`` = pricing (default).
        language: One-letter SAP language key, ``"E"`` = English.
        limit: Rows per page in list mode (max 1000).
        offset: Rows to skip, for paging (``next_offset`` in the answer).
    """
    return _safe(lambda: config_tables.condition_types(
        _get_client(), application=application, usage=usage,
        condition_type=condition_type, search=search, language=language,
        limit=limit, offset=offset,
    ))


@mcp.tool()
def pricing_procedure(
    name: str,
    application: str = "",
    usage: str = "A",
    language: str = "E",
) -> str:
    """Steps of a pricing / calculation procedure (as in V/08 or M/08).

    Reads T683S with the procedure's description (T683U) and each condition
    type's text. Rows are in step / counter order with only filled fields;
    ``field_legend`` explains the columns (requirement, account key,
    statistical, subtotal, from/to step, ...).

    Args:
        name: Procedure, e.g. ``"ZRVAA1"`` or a tax procedure like ``"TAXINN"``.
        application: ``"V"`` sales, ``"M"`` purchasing, ``"TX"`` tax. Empty
            searches all applications.
        usage: Condition usage, ``"A"`` = pricing (default).
        language: One-letter SAP language key, ``"E"`` = English.
    """
    return _safe(lambda: config_tables.pricing_procedure(
        _get_client(), name, application=application, usage=usage, language=language,
    ))


@mcp.tool()
def account_determination(
    transaction: str = "WRX",
    chart_of_accounts: str = "",
    valuation_class: str = "",
    account_modifier: str = "",
    valuation_modifier: str = "",
    language: str = "E",
) -> str:
    """MM automatic account determination (OBYC / table T030) with G/L names.

    Returns a table (``columns`` + ``rows``) plus ``account_names`` per chart.
    By default only the configured chart(s) of accounts (SAP_MCP_CHARTS or
    "charts" in the site settings) are shown, leaving out SAP sample charts.

    Args:
        transaction: Transaction key, e.g. ``"WRX"`` (GR/IR), ``"BSX"``
            (inventory), ``"GBB"`` (offsetting entry), ``"PRD"`` (price
            difference), ``"KDM"`` (exchange rate difference).
        chart_of_accounts: Leave empty for chart 1000 (the company's only
            chart). ``"*"`` = every chart, including SAP sample charts; only
            use it when explicitly asked.
        valuation_class: Optional valuation class (BKLAS), e.g. ``"3000"``.
        account_modifier: Optional account modifier (KOMOK), e.g. ``"VBR"``.
        valuation_modifier: Optional valuation grouping code (BWMOD).
        language: One-letter SAP language key for account names.
    """
    return _safe(lambda: config_tables.account_determination(
        _get_client(), transaction=transaction, chart_of_accounts=chart_of_accounts,
        valuation_class=valuation_class, account_modifier=account_modifier,
        valuation_modifier=valuation_modifier, language=language,
    ))


@mcp.tool()
def find_sap_reports(pattern: str = "Z*", top: int = 200) -> str:
    """Find SAP reports by name pattern: executable programs and the
    transaction codes that start them, with titles. E.g. ``"ZFI*"``, ``"*AGEING*"``.
    """
    return _safe(lambda: reports.list_reports(_get_client(), pattern, top))


@mcp.tool()
def sap_report_info(report: str) -> str:
    """A report's selection-screen fields (parameters and select-options) and
    its saved variants. ``report`` may be a transaction code or a program name.
    Call before run_sap_report to know what to fill in.
    """
    return _safe(lambda: reports.report_info(_get_client(), report))


@mcp.tool()
def run_sap_report(
    report: str,
    variant: str = "",
    selections: Optional[dict] = None,
    max_rows: int = 5000,
    folder: str = "",
) -> str:
    """Run an SAP GUI report and export its output, e.g. the company's own Z
    reports (ZFI_DEBTORS_AGEING, ZFI_VEND_CUST_STMT, ...).

    ALV output is captured as data with the report's own column headings and
    saved as an Excel file (Downloads, folder "SAP reports"); classic list output is
    saved as text. The answer has the file path, the columns and a preview;
    read the saved file for the rest.

    Before running a report that may post, change or send anything (anything
    other than a display / list report), ASK THE USER FIRST: this runs the
    real report in SAP under their user.

    Args:
        report: Transaction code (``"ZFI_DEBTORS_AGEING"``) or program name
            (``"ZFI_DEBTORS_AGEING_RPT"``); a transaction code runs its program.
        variant: A saved selection variant (see sap_report_info): the easiest
            way to reproduce exactly what users run.
        selections: Selection values by field name, overriding / adding to the
            variant: ``{"S_BUKRS": "1000", "P_KEYDT": "20260930",
            "S_KUNNR": ["1000", "1001"], "S_BUDAT": {"low": "20260401",
            "high": "20260930"}}``. Internal formats: dates YYYYMMDD,
            leading zeros on numbers.
        max_rows: Row limit (max 5000).
        folder: Save here instead of the default folder.

    Reports that open popups cannot run this way, and a run must finish
    within about 55 seconds.
    """
    return _safe(lambda: reports.run_report(
        _get_client(), report, variant=variant, selections=selections,
        max_rows=max_rows, folder=folder))


@mcp.tool()
def get_attachments(
    kind: str = "",
    number: str = "",
    year: str = "",
    company_code: str = "",
    object_type: str = "",
    object_key: str = "",
    download: bool = True,
    folder: str = "",
) -> str:
    """GOS attachments ("Services for Object" files) of an SAP document.

    Lists what is attached and, by default, saves each file to a folder on
    this PC (``Downloads\\SAP attachments\\<object>``). File content is NOT
    returned in the answer, only names, sizes and the saved paths; open the
    saved file to read it.

    Args:
        kind: ``"invoice"`` (MIRO, needs year), ``"material_doc"`` (MIGO, needs
            year), ``"po"``, or ``"fi"`` (accounting document, needs year and
            company_code).
        number: Document number, e.g. ``"5100067620"``.
        year: Fiscal / material document year, e.g. ``"2026"``.
        company_code: For ``kind="fi"``, e.g. ``"1000"``.
        object_type / object_key: Raw GOS object instead of kind/number, e.g.
            ``"BUS2091"`` + service entry sheet number.
        download: False lists the attachments without saving them.
        folder: Save here instead of the default folder.
    """
    return _safe(lambda: attachments.get_attachments(
        _get_client(), kind=kind, number=number, year=year,
        company_code=company_code, object_type=object_type,
        object_key=object_key, download_files=download, folder=folder or None,
    ))


@mcp.tool()
def get_sap_source(name: str, object_type: str = "", with_includes: bool = False, folder: str = "") -> str:
    """ABAP source code of a program, include, class, interface or function
    module, or of the program behind a transaction code.

    SAP resolves the name and checks the user's S_DEVELOP display
    authorization. The code is saved as .abap files under
    ``Downloads/SAP source``; small sources also come back inline, large
    ones as a preview plus the file path.

    Args:
        name: Object name or transaction code, e.g. ``"ZFI_DEBTORS_AGEING"``,
            ``"ZCL_INVOICE_PRINT"``, ``"Z_MIRO_PRINT"``.
        object_type: Optional ``PROG`` | ``INCL`` | ``CLAS`` | ``INTF`` |
            ``FUNC`` | ``TCODE`` when the name is ambiguous.
        with_includes: Also fetch the program's customer includes (Z*, Y*,
            namespaces). SAP's own includes are only listed.
        folder: Save here instead of the default folder.
    """
    return _safe(lambda: source.get_source(
        _get_client(), name, object_type=object_type, with_includes=with_includes, folder=folder))


@mcp.tool()
def get_smartform(name: str, folder: str = "", top: int = 200) -> str:
    """A Smart Form's structure and definition, for print-form development.

    With a form name: outline (pages, windows, text / code / table nodes with
    captions, text previews and program-line counts), the generated function
    module, and the complete form XML saved to ``Downloads/SAP source/smartforms``.
    With a pattern (``"ZMR*"``): the matching forms. Read-only: editing a form
    is done in the Form Builder (or the sap-write plugin).

    Args:
        name: Form name (``"ZMR_INVOICE"``) or pattern (``"Z*INV*"``).
        folder: Save the XML here instead of the default folder.
        top: Maximum forms in list mode.
    """
    return _safe(lambda: printforms.get_smartform(_get_client(), name, folder=folder, top=top))


@mcp.tool()
def read_sap_text(name: str = "", object: str = "TEXT", id: str = "", language: str = "", top: int = 200) -> str:
    """SAPscript / SO10 texts as print forms include them (READ_TEXT).

    With a name: the text lines, each prefixed by its format column. With a
    pattern or no name: the matching text headers.

    Args:
        name: Text name, e.g. ``"Z_INVOICE_FOOTER"``; ``"Z_INV*"`` lists.
        object: Text object, default ``"TEXT"`` (SO10 standard texts); e.g.
            ``"MATERIAL"``, ``"VBBK"`` for document texts.
        id: Text id, default ``"ST"`` for standard texts (``"GRUN"``,
            ``"0001"`` ... for object texts).
        language: One-letter SAP language key; default the logon language.
        top: Maximum headers in list mode.
    """
    return _safe(lambda: printforms.read_text(
        _get_client(), name, object=object, id=id, language=language, top=top))


@mcp.tool()
def sap_login(env: str = "prod") -> str:
    """Log in to SAP with your own user: opens a login window on this PC.

    Use when a tool reports missing or rejected SAP credentials, or when the
    user asks to log in or change their SAP password. The user types their
    user and password into the window. The password never passes through
    this chat; it is stored in the computer's keychain. Waits up to
    10 minutes for the user to finish.

    Args:
        env: ``"prod"`` (production, default) or ``"dev"`` (development).
    """
    import subprocess
    import sys

    try:
        target = envs.normalize(env)
    except envs.UnknownEnvironment as e:
        return json.dumps({"error": "unknown_environment", "message": str(e)})
    child_env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "sap_mcp.login_dialog", target],
            capture_output=True, text=True, timeout=600, env=child_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"ok": False, "message": "The login window was open for 10 minutes without a login."})
    try:
        out = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"ok": False, "message": "Login window did not report back.",
               "detail": (proc.stderr or "")[-500:]}
    if out.get("ok") and target == _active_env:
        _reset_client()
    return json.dumps(out)


#: Special-purpose OData tools from before table reads and SAP-side Compute.
#: Their descriptions alone cost ~9k tokens per conversation, and
#: read_sap_table / aggregate_sap_table / Compute / query_entity cover them, so
#: they are off unless SAP_MCP_TOOLSET=full.
_LEGACY_TOOLS = (
    "gl_line_items", "gl_account_balance", "trial_balance", "sales_by_material",
    "sales_today", "sales_mtd", "sales_ytd", "journal_entries", "vendor_master",
    "customer_master", "material_master", "gst_register", "purchase_orders",
    "purchase_requisitions", "material_documents", "supplier_invoices", "ap_open_items",
    "sales_orders", "outbound_deliveries", "ar_open_items", "material_stock",
    "maintenance_orders",
)
TOOLSET = os.environ.get("SAP_MCP_TOOLSET", "core").strip().lower()
if TOOLSET != "full":
    for _name in _LEGACY_TOOLS:
        mcp.remove_tool(_name)

# Every conversation pays for the tool list, so Claude gets the compact texts
# from tool_text.py (docstrings stay the full developer docs) and schemas
# without pydantic noise. SAP_MCP_TOOL_TEXT=full keeps the docstrings.
if os.environ.get("SAP_MCP_TOOL_TEXT", "compact").strip().lower() != "full":
    for _tool in mcp._tool_manager._tools.values():
        if _tool.name in tool_text.SHORT:
            _tool.description = tool_text.SHORT[_tool.name]
        _tool.parameters = tool_text.compact_schema(_tool.parameters)


_HELP = """sap-mcp — read-only SAP S/4HANA OData connector

Usage:
  sap-mcp                Run the MCP server (this is what Claude Desktop
                         launches automatically; it speaks JSON-RPC on
                         stdio, so don't run it by hand).
  sap-mcp setup          Interactively log in to PRODUCTION with your own SAP
                         user and store the credentials for your OS user only.
  sap-mcp setup --env dev  Same, for the Development / Testing system.
  sap-mcp setup --no-test  Skip the live SAP connection check.
  sap-mcp setup --advanced Also ask for host / client / TLS instead of the
                         company defaults.
  sap-mcp status         Show stored logins per environment (never passwords).
  sap-mcp logout [--env dev]  Remove stored SAP credentials (all, or one env).
  sap-mcp --help         Show this help.

Environments: prod (default), dev. Claude switches between them with the
switch_environment tool; set SAP_ENV=dev in the MCP config to start on dev.
"""


def main() -> None:
    import sys

    argv = sys.argv[1:]
    if argv:
        cmd = argv[0].lower()
        if cmd in ("setup", "login", "configure"):
            from .setup_cli import run_setup

            raise SystemExit(run_setup(argv[1:]))
        if cmd in ("status", "whoami"):
            from .setup_cli import print_status

            print_status()
            return
        if cmd in ("logout", "reset", "clear"):
            from .credentials import clear_credentials
            from .setup_cli import parse_env_arg

            try:
                env = parse_env_arg(argv[1:], default=None)
            except envs.UnknownEnvironment as e:
                print(e)
                raise SystemExit(2)
            removed = clear_credentials(env)
            which = "all environments" if env is None else envs.describe(env)
            print(f"Removed stored SAP credentials ({which})." if removed
                  else f"No stored credentials to remove ({which}).")
            return
        if cmd in ("-h", "--help", "help"):
            print(_HELP)
            return
        # Unknown argument: fall through to running the server so we never break
        # an MCP host that passes an unexpected flag.

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("sap-mcp starting on %s", envs.describe(_active_env))
    if os.environ.get("SAP_MCP_PLUGIN") == "1":
        # Running as the plugin: retire the old stand-alone connector, if any.
        try:
            from . import migrate

            outcome = migrate.retire_old_connector()
            if outcome["removed"] or outcome["stopped_processes"]:
                log.info("old SAP connector retired: %s", outcome)
        except Exception as e:  # noqa: BLE001 - never block start-up over this
            log.warning("could not check for the old SAP connector: %r", e)
    mcp.run()


if __name__ == "__main__":
    main()
