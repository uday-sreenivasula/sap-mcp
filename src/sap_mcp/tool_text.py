"""What Claude sees for each tool: compact descriptions.

Every conversation pays for the tool list, SAP question or not, so these
texts are kept short. The docstrings in server.py stay the full developer
documentation; this file is the model-facing summary. Keep every fact the
model needs to call a tool correctly (formats, limits, safety rules) and
drop the prose. server.py applies these after registering the tools and also
strips schema noise (per-field "title", nullable unions).
"""
from __future__ import annotations

SHORT: dict[str, str] = {
    # --- table reads and SAP-side calculations (first choice)
    "read_sap_table": (
        "Read rows of any SAP table or DB view, like SE16. FIRST CHOICE for SAP data. For totals use "
        "aggregate_sap_table; unsure of field names: describe_sap_table.\n"
        "where: ABAP Open SQL, not OData: 'quoted' literals, internal formats (leading zeros, dates "
        "'20250401'), e.g. \"KAPPL = 'M' AND KSCHL LIKE 'Z%'\". fields: comma list (keys always included); "
        "name them, wide tables get large. top max 1000; page with skip (answer gives next_skip). "
        "drop_empty omits blank fields."
    ),
    "aggregate_sap_table": (
        "Totals computed in SAP: one call instead of paging rows. measures: comma list of SUM:f, AVG:f, "
        "MIN:f, MAX:f, COUNT, COUNTD:f (results named SUM_HSL, ROW_COUNT, COUNTD_BELNR...). group_by: comma "
        "fields (empty = one row). where: ABAP Open SQL. order_by: result columns, e.g. \"SUM_HSL DESC\" with "
        "top=20. top max 1000. E.g. TB: table=ACDOCA, measures=SUM:HSL, group_by=RACCT, "
        "where=\"RLDNR = '0L' AND RBUKRS = '1000' AND GJAHR = '2026'\"."
    ),
    "find_duplicates": (
        "Groups of rows sharing the same values on `fields` (comma list), computed in SAP, most frequent "
        "first. E.g. duplicate invoices: table=RBKP, fields=LIFNR,XBLNR,RMWWR. where: ABAP Open SQL. "
        "min_count default 2; top max 1000."
    ),
    "trial_balance_by_period": (
        "Trial balance from ACDOCA computed in SAP: per G/L account opening (incl. carry-forward), period "
        "debit/credit, closing, with names. periods 1-16 (opening = periods before period_from); optional "
        "account range; ledger default 0L. Full-year results are also saved to Excel."
    ),
    "open_items_ageing": (
        "Vendor/customer ageing computed in SAP: open items at key_date (YYYY-MM-DD, default today) per "
        "partner in buckets by days past net due (not due, 1-30, 31-60, 61-90, 91-180, >180), with names. "
        "account_type: vendor | customer. top max 1000."
    ),
    "grir_differences": (
        "GR/IR differences computed in SAP from EKBE: per PO item, GR vs IR quantity and value, only "
        "mismatches. Optional vendor, po, key_date (YYYY-MM-DD, default today). top max 1000."
    ),
    "describe_sap_table": (
        "A table's fields (name, type, length, key, description); cached 7 days. search filters by name or "
        "description; refresh re-reads from SAP."
    ),
    # --- customizing shortcuts
    "condition_types": (
        "Pricing condition types (T685/T685T/T685A) with texts, access sequence and control data. No "
        "condition_type: paged list (limit/offset; prefer search, sales has ~1000); with one: all filled "
        "fields. application: V sales, M purchasing, TX tax. usage A = pricing. language: 1-letter key."
    ),
    "pricing_procedure": (
        "Steps of a pricing procedure (T683S, as in V/08 / M/08) with texts; only filled fields, "
        "field_legend explains columns. application V/M/TX or empty for all; usage A = pricing."
    ),
    "account_determination": (
        "MM account determination (OBYC / T030) with G/L names. transaction key e.g. WRX (GR/IR), BSX "
        "(inventory), GBB (offsetting), PRD (price diff), KDM (FX diff). chart_of_accounts: empty = the "
        "configured chart(s); \"*\" = all incl. SAP sample charts (only if asked). Optional valuation_class "
        "(BKLAS), account_modifier (KOMOK), valuation_modifier (BWMOD)."
    ),
    # --- SAP GUI reports
    "find_sap_reports": (
        "Find reports by name pattern (programs and their transaction codes, with titles), e.g. \"ZFI*\", "
        "\"*AGEING*\"."
    ),
    "sap_report_info": (
        "A report's selection fields and saved variants (report = tcode or program). Call before "
        "run_sap_report."
    ),
    "run_sap_report": (
        "Run an SAP GUI report (tcode or program) and save its output: ALV to Excel with the report's own "
        "headings (Downloads/SAP reports), classic lists to text. Returns path, columns, preview; read the "
        "file for the rest. ASK THE USER FIRST before any report that may post, change or send anything. "
        "variant: saved variant (easiest). selections override it: {\"S_BUKRS\": \"1000\", \"P_KEYDT\": "
        "\"20260930\", \"S_KUNNR\": [\"1000\",\"1001\"], \"S_BUDAT\": {\"low\": \"20260401\", \"high\": "
        "\"20260930\"}}, internal formats. max_rows max 5000. No popups; must finish in ~55 s."
    ),
    # --- attachments
    "get_attachments": (
        "GOS attachments of an SAP document: lists them and saves the files to Downloads/SAP attachments "
        "(content is never returned; open the saved file). kind: invoice (MIRO, needs year), material_doc "
        "(needs year), po, fi (needs year + company_code). Or raw object_type + object_key (e.g. BUS2091). "
        "download=false only lists."
    ),
    # --- development: code, print forms, texts
    "get_sap_source": (
        "ABAP source of a program, include, class, interface or function module, or the program behind a "
        "tcode (SAP resolves the name; S_DEVELOP display check). Saved as .abap under Downloads/SAP source; "
        "small sources inline, large ones preview + path. object_type PROG|INCL|CLAS|INTF|FUNC|TCODE if "
        "ambiguous; with_includes also fetches customer (Z/Y/namespace) includes."
    ),
    "get_smartform": (
        "Smart Form for print-form work: outline (pages > windows > text/code/table nodes with captions, "
        "text previews, program-line counts), generated function module, full form XML saved under "
        "Downloads/SAP source/smartforms. name with * lists forms. Read-only."
    ),
    "read_sap_text": (
        "SO10 / SAPscript text lines (READ_TEXT), each prefixed by its format column (* paragraph, / new "
        "line, = continue). object default TEXT, id default ST, language default logon. name with * or "
        "empty lists headers."
    ),
    # --- OData (only when clearly better than a table read)
    "list_services": "Search the Gateway catalog for OData services by name/title substring. Prefer a search term over paging.",
    "get_service_metadata": (
        "OData service structure: entity sets, or one entity's keys and fields (cached 7 days). raw=true "
        "returns the full EDMX (very large; avoid). refresh re-downloads."
    ),
    "query_entity": "Generic OData v2 query ($select/$filter/$orderby/$top/$skip/$expand) on a service's entity set.",
    # --- session
    "sap_login": (
        "Opens a login window on the user's screen for their own SAP user (password never in chat; kept in "
        "the OS keychain). Use when a tool reports missing or rejected credentials, or when asked. "
        "env: prod (default) | dev. Waits up to 10 min."
    ),
    "whoami": "Active SAP system, host, client and user.",
    "list_environments": "SAP systems available, which is active, and login status. No SAP call.",
    "switch_environment": (
        "Point all SAP tools at prod or dev (also production/development/test/SID) for this session. "
        "Non-prod answers carry _sap_env: never report them as live books. Same env = reload login."
    ),
    "token_budget": (
        "SAP data pulled in this conversation, per tool, and the limits. No SAP call. reset=true asks the "
        "USER in a window for more allowance."
    ),
}


def compact_schema(node):
    """Drop pydantic noise from an input schema without changing its meaning:
    "title" keys and nullable unions ({"anyOf": [{X}, {"type": "null"}]} -> X)."""
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "title" and isinstance(value, str):
                continue
            out[key] = compact_schema(value)
        any_of = out.get("anyOf")
        if isinstance(any_of, list):
            rest = [s for s in any_of if s != {"type": "null"}]
            if len(rest) == 1 and len(rest) < len(any_of):
                out.pop("anyOf")
                out = {**rest[0], **out}
        return out
    if isinstance(node, list):
        return [compact_schema(v) for v in node]
    return node
