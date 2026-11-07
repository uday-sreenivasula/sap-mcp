"""SAP-side calculations through ZCFG_READ_SRV Compute."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

COMPUTE = "https://sap.test:44300/sap/opu/odata/sap/ZCFG_READ_SRV/Compute"


def _call(tool, **kwargs):
    return getattr(tool, "fn", tool)(**kwargs)


def _unquote(v: str) -> str:
    return v[1:-1].replace("''", "'") if v.startswith("'") else v


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("SAP_MCP_REPORTS_DIR", str(tmp_path / "reports"))
    for k, v in {"SAP_BASE_URL": "https://sap.test:44300", "SAP_CLIENT": "100",
                 "SAP_USERNAME": "U", "SAP_PASSWORD": "pw", "SAP_VERIFY_TLS": "false"}.items():
        monkeypatch.setenv(k, v)
    import sap_mcp.server as srv
    srv._client = None
    srv._spent_chars = 0
    yield
    srv._client = None


def sap(rows):
    """Mock Compute: records (operation, params) and answers with `rows` (raw JSON text each)."""
    seen = []

    def handler(request):
        q = {k: _unquote(v) for k, v in request.url.params.items()}
        seen.append((q["Operation"], json.loads(q["Params"])))
        return httpx.Response(200, json={"d": {"results": [
            {"RowNo": i + 1, "Data": r} for i, r in enumerate(rows)]}})

    mock = respx.mock()
    mock.start()
    mock.get(url__startswith=COMPUTE).mock(side_effect=handler)
    return mock, seen


def test_aggregate_params_order_and_exact_amounts():
    from sap_mcp.server import aggregate_sap_table
    mock, seen = sap(['{"RACCT":"0001010000","SUM_HSL":123456789012.34,"ROW_COUNT":42}',
                      '{"RACCT":"0002009000","SUM_HSL":-0.10,"ROW_COUNT":1}'])
    try:
        out = json.loads(_call(aggregate_sap_table, table="acdoca", measures="sum:hsl, count",
                               group_by="racct", where="RBUKRS = '1000'", order_by="SUM_HSL DESC", top=20))
    finally:
        mock.stop()
    op, p = seen[0]
    assert op == "AGGREGATE"
    assert p == {"TABLE_NAME": "ACDOCA", "WHERE_COND": "RBUKRS = '1000'", "GROUP_BY": "RACCT",
                 "MEASURES": "SUM:HSL,COUNT", "ORDER_BY": "SUM_HSL DESC", "MAX_ROWS": 21}
    assert out["columns"] == ["RACCT", "SUM_HSL", "ROW_COUNT"]
    assert out["rows"][0] == ["0001010000", "123456789012.34", 42]
    assert out["rows"][1][1] == "-0.10"


def test_aggregate_totals_and_bad_measure():
    from sap_mcp.server import aggregate_sap_table
    mock, _ = sap(['{"ROW_COUNT":7}'])
    try:
        assert json.loads(_call(aggregate_sap_table, table="T001"))["totals"] == {"ROW_COUNT": 7}
        assert json.loads(_call(aggregate_sap_table, table="T001", measures="MEDIAN:X"))["error"] == "invalid_argument"
    finally:
        mock.stop()


def test_find_duplicates():
    from sap_mcp.server import find_duplicates
    mock, seen = sap(['{"LIFNR":"0000100045","XBLNR":"INV-77","RMWWR":11800.00,"ROW_COUNT":2}'])
    try:
        out = json.loads(_call(find_duplicates, table="RBKP", fields="lifnr,xblnr,rmwwr",
                               where="BUKRS = '1000'"))
    finally:
        mock.stop()
    assert seen[0][0] == "DUPLICATES" and seen[0][1]["FIELDS"] == "LIFNR,XBLNR,RMWWR"
    assert seen[0][1]["MIN_COUNT"] == 2
    assert out["columns"] == ["LIFNR", "XBLNR", "RMWWR", "ROW_COUNT"]
    assert out["rows"][0][-1] == 2


def test_trial_balance_by_period_totals():
    from sap_mcp.server import trial_balance_by_period
    mock, seen = sap([
        '{"RACCT":"0001010000","TXT50":"GR/IR","OPENING":100.00,"DEBIT":50.00,"CREDIT":-30.00,"CLOSING":120.00}',
        '{"RACCT":"0004000000","TXT50":"Sales","OPENING":0,"DEBIT":0,"CREDIT":-120.00,"CLOSING":-120.00}'])
    try:
        out = json.loads(_call(trial_balance_by_period, company_code="1000", fiscal_year="2026",
                               period_from=4, period_to=6))
    finally:
        mock.stop()
    assert seen[0] == ("TRIAL_BALANCE", {"COMPANY_CODE": "1000", "FISCAL_YEAR": "2026",
                                         "PERIOD_FROM": 4, "PERIOD_TO": 6, "LEDGER": "0L"})
    assert out["accounts"] == 2 and out["periods"] == "004-006"
    assert out["totals"]["CLOSING"] == "0.00"
    assert out["columns"][0] == "RACCT"
    from openpyxl import load_workbook
    ws = load_workbook(out["saved_to"]).active
    assert [c.value for c in ws[1]] == ["G/L account", "Account name", "Opening", "Debit", "Credit", "Closing"]
    assert ws.max_row == 3


def test_large_trial_balance_is_summarised_with_excel(monkeypatch):
    import sap_mcp.compute as cmp
    from sap_mcp.server import trial_balance_by_period
    monkeypatch.setattr(cmp, "TB_INLINE_ROWS", 1)
    mock, _ = sap(['{"RACCT":"1","OPENING":1}', '{"RACCT":"2","OPENING":2}', '{"RACCT":"3","OPENING":3}'])
    try:
        out = json.loads(_call(trial_balance_by_period, company_code="1000", fiscal_year="2026"))
    finally:
        mock.stop()
    assert out["accounts"] == 3 and len(out["rows"]) == 1 and "Excel" in out["note"]
    assert out["totals"]["OPENING"] == "6"


def test_ageing_maps_vendor_and_date():
    from sap_mcp.server import open_items_ageing
    mock, seen = sap(['{"PARTNER":"0000100045","NAME1":"ACME","TOTAL":-5000.00,"NOT_DUE":-1000.00,'
                      '"D001_030":-4000.00,"D031_060":0,"D061_090":0,"D091_180":0,"OVER_180":0}'])
    try:
        out = json.loads(_call(open_items_ageing, account_type="vendor", company_code="1000",
                               key_date="2026-09-30"))
    finally:
        mock.stop()
    assert seen[0][1]["ACCOUNT_TYPE"] == "K" and seen[0][1]["KEY_DATE"] == "20260930"
    assert out["totals"]["TOTAL"] == "-5000.00"
    assert out["rows"][0][1] == "ACME"


def test_ageing_rejects_bad_input():
    from sap_mcp.server import open_items_ageing
    assert json.loads(_call(open_items_ageing, account_type="bank", company_code="1000"))["error"] == "invalid_argument"
    assert json.loads(_call(open_items_ageing, account_type="customer", company_code="1000",
                            key_date="30/09/2026"))["error"] == "invalid_argument"


def test_grir():
    from sap_mcp.server import grir_differences
    mock, seen = sap(['{"EBELN":"4500001234","EBELP":"00010","LIFNR":"0000100045","GR_QTY":10,"IR_QTY":8,'
                      '"GR_VALUE":1000.00,"IR_VALUE":800.00,"DIFF_VALUE":200.00}'])
    try:
        out = json.loads(_call(grir_differences, company_code="1000", vendor="100045"))
    finally:
        mock.stop()
    assert seen[0][0] == "GRIR" and seen[0][1]["VENDOR"] == "100045"
    assert out["total_difference"] == "200.00" and out["items_with_difference"] == 1
