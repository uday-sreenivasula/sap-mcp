"""SAP GUI reports through Compute LIST_REPORTS / REPORT_INFO / RUN_REPORT."""
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
    for k, v in {"SAP_BASE_URL": "https://sap.test:44300", "SAP_CLIENT": "100",
                 "SAP_USERNAME": "U", "SAP_PASSWORD": "pw", "SAP_VERIFY_TLS": "false",
                 "SAP_MCP_REPORTS_DIR": str(tmp_path / "reports")}.items():
        monkeypatch.setenv(k, v)
    import sap_mcp.server as srv
    srv._client = None
    srv._spent_chars = 0
    yield
    srv._client = None


@pytest.fixture
def sap():
    state = {"seen": [], "rows": []}

    def handler(request):
        q = {k: _unquote(v) for k, v in request.url.params.items()}
        state["seen"].append((q["Operation"], json.loads(q["Params"])))
        return httpx.Response(200, json={"d": {"results": [
            {"RowNo": i, "Data": json.dumps(r)} for i, r in enumerate(state["rows"])]}})

    with respx.mock:
        respx.get(url__startswith=COMPUTE).mock(side_effect=handler)
        yield state


ALV = [
    {"COLUMNS": [{"FIELD": "KUNNR", "TEXT": "Customer", "HIDDEN": False},
                 {"FIELD": "NAME1", "TEXT": "Name", "HIDDEN": False},
                 {"FIELD": "DMBTR", "TEXT": "Amount", "HIDDEN": False},
                 {"FIELD": "COLOR", "TEXT": "", "HIDDEN": True}]},
    {"KUNNR": "0000001000", "NAME1": "ACME", "DMBTR": 1500.50, "COLOR": "C110"},
    {"KUNNR": "0000001001", "NAME1": "BETA", "DMBTR": 250.00, "COLOR": ""},
]


def test_run_alv_report_saves_excel_with_headings(sap):
    from openpyxl import load_workbook
    from sap_mcp.server import run_sap_report
    sap["rows"] = ALV
    out = json.loads(_call(run_sap_report, report="zfi_debtors_ageing", variant="test 180226",
                           selections={"S_BUKRS": "1000", "S_BUDAT": {"low": "20260401", "high": "20260930"},
                                       "S_KUNNR": ["1000", "1001"]}))
    op, p = sap["seen"][0]
    assert op == "RUN_REPORT" and p["REPORT"] == "ZFI_DEBTORS_AGEING" and p["VARIANT"] == "TEST 180226"
    assert p["SELECTIONS"] == [
        {"SELNAME": "S_BUKRS", "LOW": "1000"},
        {"SELNAME": "S_BUDAT", "LOW": "20260401", "HIGH": "20260930"},
        {"SELNAME": "S_KUNNR", "LOW": "1000"}, {"SELNAME": "S_KUNNR", "LOW": "1001"}]
    assert out["output"] == "ALV" and out["rows"] == 2
    assert out["columns"] == ["Customer", "Name", "Amount"]          # hidden COLOR dropped
    ws = load_workbook(out["saved_to"]).active
    assert [c.value for c in ws[1]] == ["Customer", "Name", "Amount"]
    assert [c.value for c in ws[2]] == ["0000001000", "ACME", 1500.5]


def test_run_list_report_saves_text(sap):
    from sap_mcp.server import run_sap_report
    sap["rows"] = [{"LINE": "Vendor statement   1000"}, {"LINE": "  100045   ACME     11,800.00"}]
    out = json.loads(_call(run_sap_report, report="ZFI_VEND_CUST_STMT"))
    assert out["output"] == "list" and out["lines"] == 2
    assert open(out["saved_to"], encoding="utf-8").read().splitlines()[1] == "  100045   ACME     11,800.00"


def test_report_info_splits_selections_and_variants(sap):
    from sap_mcp.server import sap_report_info
    sap["rows"] = [{"KIND": "SELECT-OPTION", "NAME": "S_BUKRS", "TYPE": "BUKRS", "TEXT": "Company code"},
                   {"KIND": "PARAMETER", "NAME": "P_KEYDT", "TYPE": "", "TEXT": "Key date"},
                   {"KIND": "VARIANT", "NAME": "TEST 180226", "TEXT": "Prod test"}]
    out = json.loads(_call(sap_report_info, report="ZFI_DEBTORS_AGEING"))
    assert [s["name"] for s in out["selections"]] == ["S_BUKRS", "P_KEYDT"]
    assert out["variants"] == [{"variant": "TEST 180226", "text": "Prod test"}]


def test_find_reports_pattern(sap):
    from sap_mcp.server import find_sap_reports
    sap["rows"] = [{"KIND": "REPORT", "NAME": "ZFI_DEBTORS_AGEING_RPT", "TEXT": "Debtors ageing"},
                   {"KIND": "TCODE", "NAME": "ZFI_DEBTORS_AGEING", "TYPE": "ZFI_DEBTORS_AGEING_RPT",
                    "TEXT": "Debtors Ageing"}]
    out = json.loads(_call(find_sap_reports, pattern="zfi*"))
    assert sap["seen"][0][1]["REPORT"] == "ZFI*"
    assert out["reports"] == [{"report": "ZFI_DEBTORS_AGEING_RPT", "title": "Debtors ageing"}]
    assert out["transactions"] == [{"tcode": "ZFI_DEBTORS_AGEING", "program": "ZFI_DEBTORS_AGEING_RPT",
                                    "title": "Debtors Ageing"}]
    assert json.loads(_call(find_sap_reports, pattern="Z; DROP"))["error"] == "invalid_argument"
