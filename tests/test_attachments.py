"""get_attachments against a mocked ZCFG_READ_SRV."""
from __future__ import annotations

import base64
import json

import httpx
import pytest
import respx

from sap_mcp.attachments import gos_key

ROOT = "https://sap.test:44300/sap/opu/odata/sap/ZCFG_READ_SRV"
PDF = b"%PDF-1.7 fake invoice bytes \x00\x01\x02"
DOC = "FOL40000000000004EXT40000000001234"


def _call(tool, **kwargs):
    return getattr(tool, "fn", tool)(**kwargs)


def _unquote(v: str) -> str:
    return v[1:-1].replace("''", "'") if v.startswith("'") else v


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k, v in {"SAP_BASE_URL": "https://sap.test:44300", "SAP_CLIENT": "100",
                 "SAP_USERNAME": "U", "SAP_PASSWORD": "pw", "SAP_VERIFY_TLS": "false",
                 "SAP_MCP_ATTACHMENTS_DIR": str(tmp_path / "att")}.items():
        monkeypatch.setenv(k, v)
    import sap_mcp.server as srv
    srv._client = None
    srv._spent_chars = 0
    yield
    srv._client = None


@pytest.fixture
def calls():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = {k: _unquote(v) for k, v in request.url.params.items()}
        assert request.url.path.endswith("/Compute")
        op, p = q["Operation"], json.loads(q["Params"])
        seen.append((op, p))
        row = {"DOC_ID": DOC, "RELATION": "ATTA", "FILE_NAME": "ACME SUPPLIES 0054.pdf",
               "EXTENSION": "pdf", "SIZE_BYTES": len(PDF), "DESCRIPTION": "ACME",
               "CREATED_BY": "BTFI15", "CREATED_ON": "20260707101500", "CONTENT": ""}
        if p["OBJECT_KEY"] != "51000676202026":
            return httpx.Response(200, json={"d": {"results": []}})
        if op == "GET_ATTACHMENT":
            assert p["DOC_ID"] == DOC
            row["CONTENT"] = base64.b64encode(PDF).decode()
        return httpx.Response(200, json={"d": {"results": [{"RowNo": 1, "Data": json.dumps(row)}]}})

    with respx.mock:
        respx.get(url__startswith=ROOT).mock(side_effect=handler)
        yield seen


def test_gos_keys():
    assert gos_key("invoice", "5100067620", "2026") == ("BUS2081", "51000676202026")
    assert gos_key("material_doc", "5000001234", "2026") == ("BUS2017", "50000012342026")
    assert gos_key("po", "4500012345") == ("BUS2012", "4500012345")
    assert gos_key("fi", "1900000012", "2026", "1000") == ("BKPF", "100019000000122026")
    assert gos_key("po", "123") == ("BUS2012", "0000000123")
    with pytest.raises(ValueError):
        gos_key("invoice", "5100067620")


def test_downloads_file_and_keeps_content_out_of_the_answer(calls, tmp_path):
    from sap_mcp.server import get_attachments
    raw = _call(get_attachments, kind="invoice", number="5100067620", year="2026")
    out = json.loads(raw)
    assert out["count"] == 1
    att = out["attachments"][0]
    assert att["file_name"] == "ACME SUPPLIES 0054.pdf"
    assert att["created_by"] == "BTFI15" and att["created_on"] == "07.07.2026"
    saved = att["saved_to"]
    assert open(saved, "rb").read() == PDF
    assert base64.b64encode(PDF).decode() not in raw
    assert [c[0] for c in calls] == ["LIST_ATTACHMENTS", "GET_ATTACHMENT"]


def test_list_only_does_not_fetch_content(calls):
    from sap_mcp.server import get_attachments
    out = json.loads(_call(get_attachments, kind="invoice", number="5100067620",
                           year="2026", download=False))
    assert out["count"] == 1 and "saved_to" not in out["attachments"][0]
    assert [c[0] for c in calls] == ["LIST_ATTACHMENTS"]


def test_no_attachments(calls):
    from sap_mcp.server import get_attachments
    out = json.loads(_call(get_attachments, kind="po", number="4500000001"))
    assert out["count"] == 0 and "No GOS attachments" in out["note"]


def test_bad_kind_is_invalid_argument(calls):
    from sap_mcp.server import get_attachments
    out = json.loads(_call(get_attachments, kind="ses", number="1"))
    assert out["error"] == "invalid_argument"


def test_older_service_without_function_says_so():
    from sap_mcp.server import get_attachments
    body = {"error": {"message": {"value": "Resource not found for the segment 'Compute'."}}}
    with respx.mock:
        respx.get(url__startswith=ROOT).mock(return_value=httpx.Response(404, json=body))
        out = json.loads(_call(get_attachments, kind="po", number="4500000001"))
    assert "older version without 'Compute'" in out["message"]
