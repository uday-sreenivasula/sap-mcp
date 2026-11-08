"""ABAP source, Smart Forms and SO10 texts through Compute GET_SOURCE / GET_SMARTFORM / READ_TEXT."""
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
                 "SAP_MCP_SOURCE_DIR": str(tmp_path / "source")}.items():
        monkeypatch.setenv(k, v)
    import sap_mcp.server as srv
    srv._client = None
    srv._spent_chars = 0
    yield
    srv._client = None


@pytest.fixture
def sap():
    state = {"seen": [], "rows": [], "error": None}

    def handler(request):
        q = {k: _unquote(v) for k, v in request.url.params.items()}
        state["seen"].append((q["Operation"], json.loads(q["Params"])))
        if state["error"]:
            return httpx.Response(400, json={"error": {"message": {"value": state["error"]}}})
        return httpx.Response(200, json={"d": {"results": [
            {"RowNo": i, "Data": json.dumps(r)} for i, r in enumerate(state["rows"])]}})

    with respx.mock:
        respx.get(url__startswith=COMPUTE).mock(side_effect=handler)
        yield state


PROG = "REPORT zfi_test.\nINCLUDE zfi_test_top.\nWRITE 'x'."


def test_small_program_comes_inline_and_is_saved(sap, tmp_path):
    from sap_mcp.server import get_sap_source
    sap["rows"] = [
        {"KIND": "SOURCE", "NAME": "ZFI_TEST", "TYPE": "PROG", "TITLE": "Test", "VIA": "transaction ZFI_T",
         "LINE_COUNT": 3, "TRUNCATED": False, "SOURCE": PROG},
        {"KIND": "INCLUDE", "NAME": "ZFI_TEST_TOP", "TYPE": "INCL", "TITLE": "Top"},
        {"KIND": "INCLUDE", "NAME": "RSDBRUNT", "TYPE": "INCL", "TITLE": ""},
    ]
    out = json.loads(_call(get_sap_source, name="zfi_t"))
    assert sap["seen"][0] == ("GET_SOURCE", {"REPORT": "ZFI_T", "WITH_INCLUDES": False})
    assert out["object"] == "ZFI_TEST" and out["resolved_via"] == "transaction ZFI_T"
    assert out["source"]["ZFI_TEST"] == PROG
    assert [i["name"] for i in out["includes_not_fetched"]] == ["ZFI_TEST_TOP", "RSDBRUNT"]
    assert "with_includes=true" in out["hint"]
    saved = tmp_path / "source" / "ZFI_TEST.abap"
    assert saved.read_text(encoding="utf-8") == PROG
    assert out["objects"][0]["saved_to"] == str(saved)


def test_large_source_gives_preview_and_includes_go_to_subfolder(sap, tmp_path):
    from sap_mcp.server import get_sap_source
    big = "\n".join(f"* line {i}" for i in range(2000))
    sap["rows"] = [
        {"KIND": "SOURCE", "NAME": "ZBIG", "TYPE": "PROG", "LINE_COUNT": 2000, "SOURCE": big},
        {"KIND": "SOURCE", "NAME": "ZBIG_TOP", "TYPE": "INCL", "LINE_COUNT": 1, "SOURCE": "DATA x."},
    ]
    out = json.loads(_call(get_sap_source, name="ZBIG", with_includes=True, object_type="prog"))
    assert sap["seen"][0][1] == {"REPORT": "ZBIG", "OBJECT_TYPE": "PROG", "WITH_INCLUDES": True}
    assert "source" not in out and out["preview"].startswith("* line 0\n")
    assert (tmp_path / "source" / "ZBIG" / "ZBIG_TOP.abap").read_text(encoding="utf-8") == "DATA x."
    assert len(out["objects"]) == 2


def test_bad_object_type_and_name_are_rejected_without_sap():
    from sap_mcp.server import get_sap_source
    assert json.loads(_call(get_sap_source, name="ZX", object_type="TABLE"))["error"] == "invalid_argument"
    assert json.loads(_call(get_sap_source, name="drop table;"))["error"] == "invalid_argument"


def test_old_service_without_get_source_says_so(sap):
    from sap_mcp.server import get_sap_source
    sap["error"] = "Unknown Operation 'GET_SOURCE'. Use AGGREGATE, ..."
    out = json.loads(_call(get_sap_source, name="ZFI_TEST"))
    assert out["error"] == "sap" and "older version without GET_SOURCE" in out["message"]


SF_XML = """<?xml version="1.0"?><smartform><sf:SMARTFORM xmlns:sf="urn:sap-com:SmartForms:2000:internal-structure">
<HEADER><FORMNAME>ZMR_INVOICE</FORMNAME></HEADER>
<PAGE><INAME>FIRST</INAME><NODETYPE>PA</NODETYPE><CAPTION>First page</CAPTION>
  <WINDOW><INAME>MAIN</INAME><NODETYPE>WI</NODETYPE><CAPTION>Main window</CAPTION>
    <TEXT><INAME>HEADER_TXT</INAME><NODETYPE>TE</NODETYPE><CAPTION>Heading</CAPTION>
      <ITFTEXT><item><TDFORMAT>*</TDFORMAT><TDLINE>Tax Invoice</TDLINE></item>
               <item><TDFORMAT>/</TDFORMAT><TDLINE>&amp;GV_NUMBER&amp;</TDLINE></item></ITFTEXT></TEXT>
    <CODE><INAME>CALC</INAME><NODETYPE>CO</NODETYPE><CAPTION>Totals</CAPTION>
      <CODE><item>gv_total = gv_net + gv_tax.</item><item>CONDENSE gv_total.</item></CODE></CODE>
  </WINDOW>
</PAGE></sf:SMARTFORM></smartform>"""


def test_smartform_outline_and_xml_file(sap, tmp_path):
    from sap_mcp.server import get_smartform
    sap["rows"] = [{"KIND": "SMARTFORM", "NAME": "ZMR_INVOICE", "TITLE": "MIRO invoice", "LANGUAGE": "E",
                    "FUNCTION": "/1BCDWB/SF00000123", "CHANGED_BY": "DEV1", "CHANGED_ON": "2026-10-01",
                    "XML": SF_XML}]
    out = json.loads(_call(get_smartform, name="zmr_invoice"))
    assert sap["seen"][0] == ("GET_SMARTFORM", {"REPORT": "ZMR_INVOICE", "MAX_ROWS": 200})
    assert out["function_module"] == "/1BCDWB/SF00000123"
    lines = out["outline"].splitlines()
    assert lines[0] == "PAGE FIRST: First page"
    assert lines[1] == "  WINDOW MAIN: Main window"
    assert lines[2].startswith("    TEXT HEADER_TXT: Heading  [2 text lines: 'Tax Invoice'")
    assert lines[3] == "    CODE CALC: Totals  [2 program lines]"
    assert out["nodes"] == {"PAGE": 1, "WINDOW": 1, "TEXT": 1, "CODE": 1}
    assert (tmp_path / "source" / "smartforms" / "ZMR_INVOICE.xml").read_text(encoding="utf-8") == SF_XML


def test_smartform_pattern_lists_forms(sap):
    from sap_mcp.server import get_smartform
    sap["rows"] = [{"KIND": "FORM", "NAME": "ZMR_INVOICE", "TITLE": "MIRO invoice", "LANGUAGE": "E",
                    "CHANGED_BY": "DEV1", "CHANGED_ON": "2026-10-01"}]
    out = json.loads(_call(get_smartform, name="ZMR*", top=50))
    assert sap["seen"][0][1] == {"REPORT": "ZMR*", "MAX_ROWS": 50}
    assert out["count"] == 1 and out["forms"][0]["form"] == "ZMR_INVOICE"


def test_read_text_single_and_list(sap):
    from sap_mcp.server import read_sap_text
    sap["rows"] = [{"KIND": "TEXT", "OBJECT": "TEXT", "NAME": "Z_FOOTER", "ID": "ST", "LANGUAGE": "E",
                    "TITLE": "Footer", "LINE_COUNT": 2, "TEXT": "* Thank you\n/ Terms apply"}]
    out = json.loads(_call(read_sap_text, name="Z_FOOTER"))
    assert sap["seen"][0] == ("READ_TEXT", {"TEXT_OBJECT": "TEXT", "TEXT_NAME": "Z_FOOTER", "MAX_ROWS": 200})
    assert out["text"] == "* Thank you\n/ Terms apply" and out["lines"] == 2

    sap["rows"] = [{"KIND": "TEXT_HEADER", "OBJECT": "TEXT", "NAME": "Z_FOOTER", "ID": "ST", "LANGUAGE": "E",
                    "TITLE": "Footer", "CHANGED_BY": "DEV1", "CHANGED_ON": "2026-01-05"}]
    out = json.loads(_call(read_sap_text, name="Z_F*", language="e"))
    assert sap["seen"][1][1]["LANGUAGE"] == "E"
    assert out["count"] == 1 and out["texts"][0]["name"] == "Z_FOOTER"


def test_program_parts_texts_status_and_screens(sap, tmp_path):
    from sap_mcp.server import get_program_parts
    sap["rows"] = [
        {"KIND": "TEXT", "ID": "R", "KEY": "", "TEXT": "Vendor payment report"},
        {"KIND": "TEXT", "ID": "I", "KEY": "001", "TEXT": "Approve"},
        {"KIND": "TEXT", "ID": "S", "KEY": "S_BUKRS", "TEXT": "Company code"},
        {"KIND": "CUA", "ADM": {"PFKCODE": "000001"},
         "STA": [{"CODE": "ZSTATUS", "MODAL": "", "PFKCODE": "000001", "BUTCODE": "000001", "ACTCODE": "000001"}],
         "FUN": [{"CODE": "POST", "TEXTNO": "001", "TEXT_TYPE": "F", "TEXT": "Post payment"}],
         "TIT": [{"CODE": "T01", "TEXT": "Vendor payments"}],
         "PFK": [{"CODE": "000001", "PFNO": "08", "FUNCODE": "POST"}],
         "BUT": [], "ACT": [], "MEN": [], "MTX": [], "SET": [], "DOC": [], "BIV": []},
        {"KIND": "SCREEN", "DYNNR": "1000", "TYPE": "S", "TITLE": "Selection"},
    ]
    out = json.loads(_call(get_program_parts, name="zfi_vendor_pymt_report"))
    assert sap["seen"][0] == ("PROGRAM_PARTS", {"REPORT": "ZFI_VENDOR_PYMT_REPORT"})
    assert out["texts"] == {"title": "Vendor payment report", "text_elements": {"001": "Approve"},
                            "selection_texts": {"S_BUKRS": "Company code"}}
    assert out["gui"]["statuses"] == [{"code": "ZSTATUS", "pfkcode": "000001", "butcode": "000001",
                                       "actcode": "000001"}]
    assert out["gui"]["function_texts"][0]["text"] == "Post payment"
    assert out["screens"] == [{"screen": "1000", "type": "S", "title": "Selection"}]
    saved = json.loads((tmp_path / "source" / "ZFI_VENDOR_PYMT_REPORT.parts.json").read_text(encoding="utf-8"))
    assert saved["gui"]["function_keys"] == [{"code": "000001", "pfno": "08", "funcode": "POST"}]
