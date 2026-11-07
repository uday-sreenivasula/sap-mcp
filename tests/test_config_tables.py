"""Config-table tools against a mocked ZCFG_READ_SRV."""
from __future__ import annotations

import json

import httpx
import pytest
import respx

ROOT = "https://sap.test:44300/sap/opu/odata/sap/ZCFG_READ_SRV"

#: Fake DDIC: table -> [(field, key, type, text)]
DDIC = {
    "T685": [("MANDT", 1, "CLNT", "Client"), ("KVEWE", 1, "CHAR", "Usage"),
             ("KAPPL", 1, "CHAR", "Application"), ("KSCHL", 1, "CHAR", "Condition type"),
             ("KOZGF", 0, "CHAR", "Access sequence")],
    "T685T": [("SPRAS", 1, "LANG", "Language"), ("KVEWE", 1, "CHAR", "Usage"),
              ("KAPPL", 1, "CHAR", "Application"), ("KSCHL", 1, "CHAR", "Condition type"),
              ("VTEXT", 0, "CHAR", "Name")],
    # No KGRPE/KRUEK/KNTYP here: the shortcut must not ask for them.
    "T685A": [("MANDT", 1, "CLNT", "Client"), ("KAPPL", 1, "CHAR", "Application"),
              ("KSCHL", 1, "CHAR", "Condition type"), ("KRECH", 0, "CHAR", "Calculation type"),
              ("KOAID", 0, "CHAR", "Condition class"), ("KMANU", 0, "CHAR", "Manual entries"),
              ("KNEGA", 0, "CHAR", "Plus/minus")],
    "T030": [("MANDT", 1, "CLNT", "Client"), ("KTOPL", 1, "CHAR", "Chart"),
             ("KTOSL", 1, "CHAR", "Transaction"), ("BWMOD", 1, "CHAR", "Val. grouping"),
             ("KOMOK", 1, "CHAR", "Modifier"), ("BKLAS", 1, "CHAR", "Valuation class"),
             ("KONTS", 0, "CHAR", "Debit"), ("KONTH", 0, "CHAR", "Credit")],
    "T030W": [("SPRAS", 1, "LANG", "Language"), ("KTOSL", 1, "CHAR", "Transaction"),
              ("LTEXT", 0, "CHAR", "Text")],
    "T001": [("MANDT", 1, "CLNT", "Client"), ("BUKRS", 1, "CHAR", "Company code"),
             ("KTOPL", 0, "CHAR", "Chart")],
    "SKAT": [("SPRAS", 1, "LANG", "Language"), ("KTOPL", 1, "CHAR", "Chart"),
             ("SAKNR", 1, "CHAR", "G/L"), ("TXT50", 0, "CHAR", "Long text")],
}

DATA = {
    "T685": [{"KVEWE": "A", "KAPPL": "M", "KSCHL": "PB00", "KOZGF": "0002"},
             {"KVEWE": "A", "KAPPL": "M", "KSCHL": "ZFR1", "KOZGF": ""}],
    "T685T": [{"KSCHL": "PB00", "VTEXT": "Gross Price"}, {"KSCHL": "ZFR1", "VTEXT": "Freight"}],
    "T685A": [{"KAPPL": "M", "KSCHL": "PB00", "KRECH": "C", "KOAID": "B", "KMANU": "", "KNEGA": ""},
              {"KAPPL": "M", "KSCHL": "ZFR1", "KRECH": "B", "KOAID": "A", "KMANU": "C", "KNEGA": ""}],
    "T030": [{"KTOPL": "INT", "KTOSL": "WRX", "BWMOD": "", "KOMOK": "", "BKLAS": "",
              "KONTS": "0021100000", "KONTH": "0021100000"}],
    "T030W": [{"SPRAS": "E", "KTOSL": "WRX", "LTEXT": "GR/IR clearing"}],
    "T001": [{"BUKRS": "1000", "KTOPL": "INT"}, {"BUKRS": "2000", "KTOPL": "INT"}],
    "SKAT": [{"KTOPL": "INT", "SAKNR": "0021100000", "TXT50": "GR/IR Clearing Account"}],
}


def _call(tool, **kwargs):
    return getattr(tool, "fn", tool)(**kwargs)


def _unquote(v: str) -> str:
    return v[1:-1].replace("''", "'") if v.startswith("'") else v


class FakeService:
    def __init__(self):
        self.reads: list[dict] = []
        self.field_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        p = {k: _unquote(v) for k, v in request.url.params.items()}
        table = p.get("TableName", "")
        if request.url.path.endswith("/GetTableFields"):
            self.field_calls += 1
            rows = [
                {"FieldName": f, "Position": i + 1, "KeyFlag": "X" if key else "",
                 "DataType": t, "Length": 4, "Decimals": 0, "CheckTable": "",
                 "Description": text}
                for i, (f, key, t, text) in enumerate(DDIC.get(table, []))
            ]
            return httpx.Response(200, json={"d": {"results": rows}})
        self.reads.append(p)
        if table not in DDIC:
            return httpx.Response(400, json={"error": {"message": {"value": f"Table {table} does not exist"}}})
        known = {f for f, *_ in DDIC[table]}
        asked = [f for f in p.get("Fields", "").split(",") if f]
        bad = [f for f in asked if f not in known]
        if bad:
            return httpx.Response(400, json={"error": {"message": {"value": f"Unknown field(s) {bad}"}}})
        rows = DATA.get(table, [])
        out = [{"RowNo": i + 1, "Data": json.dumps(r)} for i, r in enumerate(rows)]
        skip = int(p.get("Skip", 0))
        page = out[skip: skip + int(p.get("MaxRows", 100))]
        return httpx.Response(200, json={"d": {"results": page}})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in {"SAP_BASE_URL": "https://sap.test:44300", "SAP_CLIENT": "100",
                 "SAP_USERNAME": "U", "SAP_PASSWORD": "pw", "SAP_VERIFY_TLS": "false"}.items():
        monkeypatch.setenv(k, v)
    import sap_mcp.config_tables as ct
    import sap_mcp.server as srv
    srv._client = None
    srv._spent_chars = 0
    ct._fields_mem.clear()
    yield
    srv._client = None
    ct._fields_mem.clear()


@pytest.fixture
def fake():
    svc = FakeService()
    with respx.mock:
        respx.get(url__startswith=ROOT).mock(side_effect=svc)
        yield svc


def test_read_sap_table_sends_abap_where_and_parses_rows(fake):
    from sap_mcp.server import read_sap_table
    out = json.loads(_call(read_sap_table, table="t685a", where="KAPPL = 'M'",
                           fields="kschl, krech", top=1))
    sent = fake.reads[-1]
    assert sent["TableName"] == "T685A"
    assert sent["Where"] == "KAPPL = 'M'"          # quotes survive the round trip
    assert sent["Fields"] == "KSCHL,KRECH"
    assert sent["MaxRows"] == "2"                   # one extra to detect more rows
    assert out["returned"] == 1
    assert out["more_available"] is True and out["next_skip"] == 1
    assert "KMANU" not in out["rows"][0]            # blank fields dropped


def test_read_sap_table_rejects_bad_names_without_calling_sap(fake):
    from sap_mcp.server import read_sap_table
    out = json.loads(_call(read_sap_table, table="T001; DROP"))
    assert out["error"] == "invalid_argument"
    assert fake.reads == []


def test_sap_error_message_reaches_the_caller(fake):
    from sap_mcp.server import read_sap_table
    out = json.loads(_call(read_sap_table, table="ZNOPE"))
    assert out["error"] == "sap"
    assert "Table ZNOPE does not exist" in out["message"]


def test_missing_service_gives_setup_pointer():
    from sap_mcp.server import read_sap_table
    with respx.mock:
        respx.get(url__startswith=ROOT).mock(return_value=httpx.Response(
            403, text="No service found for namespace '', name 'ZCFG_READ_SRV', version '0001'"))
        out = json.loads(_call(read_sap_table, table="T001"))
    assert "sap_side/README.md" in out["message"]


def test_describe_sap_table_is_cached(fake):
    from sap_mcp.server import describe_sap_table
    out = json.loads(_call(describe_sap_table, table="T030", search="credit"))
    assert [f["name"] for f in out["fields"]] == ["KONTH"]
    assert "MANDT" in out["keys"]
    _call(describe_sap_table, table="T030")
    assert fake.field_calls == 1


def test_condition_types_merges_texts_and_control_and_skips_unknown_fields(fake):
    from sap_mcp.server import condition_types
    out = json.loads(_call(condition_types, application="M"))
    assert out["total"] == 2 and "more_available" not in out
    table = out["condition_types"]
    # KNEGA is blank everywhere, so the column is dropped.
    assert table["columns"] == ["condition_type", "text", "access_sequence", "KRECH", "KOAID", "KMANU"]
    pb00, zfr1 = (dict(zip(table["columns"], r)) for r in table["rows"])
    assert pb00 == {"condition_type": "PB00", "text": "Gross Price",
                    "access_sequence": "0002", "KRECH": "C", "KOAID": "B", "KMANU": ""}
    assert zfr1["KMANU"] == "C" and zfr1["access_sequence"] == ""
    assert out["field_legend"]["KRECH"] == "Calculation type"
    t685a = next(r for r in fake.reads if r["TableName"] == "T685A")
    assert "KGRPE" not in t685a["Fields"]  # not in this system's DDIC


def test_account_determination_names_accounts(fake):
    from sap_mcp.server import account_determination
    out = json.loads(_call(account_determination, transaction="wrx"))
    assert out["transaction_text"] == "GR/IR clearing"
    assert out["columns"] == ["chart_of_accounts", "debit_account", "credit_account"]
    assert out["rows"] == [["INT", "21100000", "21100000"]]
    assert out["account_names"] == {"INT": {"21100000": "GR/IR Clearing Account"}}
    skat = next(r for r in fake.reads if r["TableName"] == "SKAT")
    assert "SAKNR IN ('0021100000')" in skat["Where"]
    # Defaults to chart 1000, with no lookup that could come back empty.
    t030 = next(r for r in fake.reads if r["TableName"] == "T030")
    assert "KTOPL IN ('1000')" in t030["Where"]
    assert out["charts_of_accounts"] == ["1000"]
    assert not any(r["TableName"] == "T001" for r in fake.reads)


def test_account_determination_star_means_all_charts(fake):
    from sap_mcp.server import account_determination
    out = json.loads(_call(account_determination, transaction="WRX", chart_of_accounts="*"))
    t030 = next(r for r in fake.reads if r["TableName"] == "T030")
    assert "KTOPL" not in t030["Where"]
    assert out["charts_of_accounts"] == "all"


def test_condition_types_pages(fake):
    from sap_mcp.server import condition_types
    out = json.loads(_call(condition_types, application="M", limit=1))
    assert out["returned"] == 1 and out["more_available"] is True and out["next_offset"] == 1
    out = json.loads(_call(condition_types, application="M", limit=1, offset=1))
    assert out["condition_types"]["rows"][0][0] == "ZFR1"


def test_single_condition_type_keeps_readable_dict(fake):
    from sap_mcp.server import condition_types
    out = json.loads(_call(condition_types, application="M", condition_type="PB00"))
    assert out["condition_types"][0]["condition_type"] == "PB00"


def test_internal_reads_follow_pages(fake, monkeypatch):
    import sap_mcp.config_tables as ct
    monkeypatch.setattr(ct, "MAX_ROWS", 1)  # force one row per page
    from sap_mcp.server import condition_types
    out = json.loads(_call(condition_types, application="M"))
    assert out["total"] == 2
    t685 = [r for r in fake.reads if r["TableName"] == "T685"]
    assert [r["Skip"] for r in t685] == ["0", "1"]

