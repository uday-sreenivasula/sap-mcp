"""Tests for the SAP OData client. SAP itself is mocked with respx, so
these run fully offline."""
from __future__ import annotations

import httpx
import pytest
import respx

from sap_mcp.config import SapConfig
from sap_mcp.sap_client import ReadOnlyViolation, SapODataClient, SapODataError


def _make_client(**overrides) -> SapODataClient:
    cfg = SapConfig(
        base_url="https://sap.test:44300",
        client="100",
        username="SVC_CLAUDE",
        password="pw",
        verify_tls=False,
        **overrides,
    )
    # Use a non-auth httpx client because respx will intercept regardless and
    # we want to be able to inspect the actual sent request.
    return SapODataClient(cfg, http_client=httpx.Client(verify=False, timeout=5))


# -------------------------------------------------------------- read-only guard


def test_post_put_patch_delete_are_blocked():
    c = _make_client()
    for verb in ("post", "put", "patch", "delete"):
        with pytest.raises(ReadOnlyViolation):
            getattr(c, verb)("anything")


# ------------------------------------------------------------------- get + auth


@respx.mock
def test_get_adds_sap_client_and_format_json():
    route = respx.get("https://sap.test:44300/sap/opu/odata/sap/SOME_SRV/Foo").mock(
        return_value=httpx.Response(200, json={"d": {"results": []}})
    )
    c = _make_client()
    out = c.get("SOME_SRV/Foo")
    assert out == {"d": {"results": []}}

    sent = route.calls.last.request
    assert sent.url.params["sap-client"] == "100"
    assert sent.url.params["$format"] == "json"


@respx.mock
def test_401_raises_authentication_error():
    respx.get("https://sap.test:44300/sap/opu/odata/sap/X/Y").mock(
        return_value=httpx.Response(401, text="<html>denied</html>")
    )
    c = _make_client()
    with pytest.raises(SapODataError) as ei:
        c.get("X/Y")
    assert ei.value.status_code == 401


@respx.mock
def test_403_raises_authorization_error():
    respx.get("https://sap.test:44300/sap/opu/odata/sap/X/Y").mock(
        return_value=httpx.Response(403)
    )
    c = _make_client()
    with pytest.raises(SapODataError) as ei:
        c.get("X/Y")
    assert ei.value.status_code == 403


# ----------------------------------------------------------------- query() args


@respx.mock
def test_query_builds_odata_url():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_Supplier"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [{"BP": "1"}]}}))
    c = _make_client()
    c.query(
        "API_BUSINESS_PARTNER",
        "A_Supplier",
        select="BusinessPartner,BusinessPartnerName",
        filter="Country eq 'IN'",
        orderby="BusinessPartner",
        top=10,
        skip=20,
    )
    p = route.calls.last.request.url.params
    assert p["$select"] == "BusinessPartner,BusinessPartnerName"
    assert p["$filter"] == "Country eq 'IN'"
    assert p["$orderby"] == "BusinessPartner"
    assert p["$top"] == "10"
    assert p["$skip"] == "20"
    assert p["sap-client"] == "100"


@respx.mock
def test_list_services_hits_catalog_endpoint():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [{"Title": "API_BUSINESS_PARTNER"}]}}))
    c = _make_client()
    out = c.list_services()
    assert out["d"]["results"][0]["Title"] == "API_BUSINESS_PARTNER"
    assert route.called


@respx.mock
def test_metadata_returns_raw_xml_when_not_json():
    xml = '<?xml version="1.0"?><edmx:Edmx />'
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_BUSINESS_PARTNER/$metadata"
    ).mock(return_value=httpx.Response(200, text=xml, headers={"content-type": "application/xml"}))
    c = _make_client()
    out = c.get_metadata("API_BUSINESS_PARTNER")
    assert out["_raw"] == xml
    assert "xml" in out["_content_type"]


# ------------------------------------------------- metadata introspection

_EDMX = """<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="1.0" xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
           xmlns:sap="http://www.sap.com/Protocols/SAPData">
  <edmx:DataServices>
    <Schema Namespace="API_BUSINESS_PARTNER" xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
      <EntityType Name="A_SupplierType" sap:label="Supplier">
        <Key><PropertyRef Name="Supplier"/></Key>
        <Property Name="Supplier" Type="Edm.String" Nullable="false" MaxLength="10"
                  sap:label="Supplier" sap:creatable="false"/>
        <Property Name="SupplierName" Type="Edm.String" MaxLength="80" sap:label="Name"/>
        <Property Name="CreationDate" Type="Edm.DateTime" sap:label="Created On"/>
      </EntityType>
      <EntityType Name="A_CustomerType">
        <Key><PropertyRef Name="Customer"/></Key>
        <Property Name="Customer" Type="Edm.String" Nullable="false" MaxLength="10"/>
      </EntityType>
      <EntityContainer Name="API_BUSINESS_PARTNER_Entities" m:IsDefaultEntityContainer="true"
                       xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
        <EntitySet Name="A_Supplier" EntityType="API_BUSINESS_PARTNER.A_SupplierType"/>
        <EntitySet Name="A_Customer" EntityType="API_BUSINESS_PARTNER.A_CustomerType"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""


def _mock_metadata():
    return respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_BUSINESS_PARTNER/$metadata"
    ).mock(return_value=httpx.Response(
        200, text=_EDMX, headers={"content-type": "application/xml"}
    ))


@respx.mock
def test_list_entity_sets_returns_names_only():
    _mock_metadata()
    out = _make_client().list_entity_sets("API_BUSINESS_PARTNER")
    assert out["entity_sets"] == ["A_Customer", "A_Supplier"]
    assert out["entity_set_count"] == 2
    # The annotation noise must not survive.
    assert "sap:label" not in str(out)


@respx.mock
def test_get_entity_schema_by_entity_set_name():
    _mock_metadata()
    out = _make_client().get_entity_schema("API_BUSINESS_PARTNER", "A_Supplier")
    assert out["keys"] == ["Supplier"]
    assert out["property_count"] == 3
    names = [p["name"] for p in out["properties"]]
    assert names == ["Supplier", "SupplierName", "CreationDate"]
    supplier = out["properties"][0]
    assert supplier["type"] == "String"
    assert supplier["required"] is True
    assert supplier["max_length"] == "10"


@respx.mock
def test_get_entity_schema_is_case_insensitive_and_accepts_type_name():
    _mock_metadata()
    c = _make_client()
    assert c.get_entity_schema("API_BUSINESS_PARTNER", "a_supplier")["keys"] == ["Supplier"]
    assert c.get_entity_schema("API_BUSINESS_PARTNER", "A_SupplierType")["keys"] == ["Supplier"]


@respx.mock
def test_get_entity_schema_unknown_entity_suggests_alternatives():
    _mock_metadata()
    out = _make_client().get_entity_schema("API_BUSINESS_PARTNER", "Supplier")
    assert "error" in out
    assert "A_Supplier" in out["did_you_mean"]


@respx.mock
def test_list_services_selects_only_needed_fields():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": []}}))
    _make_client().list_services()
    params = route.calls[0].request.url.params
    assert params["$select"] == "TechnicalServiceName,Title,TechnicalServiceVersion"
    assert params["$top"] == "500"


@respx.mock
def test_list_services_falls_back_when_select_rejected():
    """Some Gateway releases reject $select on the catalog. We must not break."""
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        if "%24select" in str(request.url) or "$select" in str(request.url):
            return httpx.Response(400, text="Query option $select is not supported")
        return httpx.Response(200, json={"d": {"results": [{"Title": "OK"}]}})

    respx.get(
        "https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
    ).mock(side_effect=_handler)

    out = _make_client().list_services()
    assert out["d"]["results"][0]["Title"] == "OK"
    assert calls["n"] == 2  # tried with $select, then without


@respx.mock
def test_query_falls_back_when_select_rejected():
    calls = {"n": 0}

    def _handler(request):
        calls["n"] += 1
        if "select" in str(request.url):
            return httpx.Response(400, text="Property Foo not found")
        return httpx.Response(200, json={"d": {"results": [{"PurchaseOrder": "1"}]}})

    respx.get(
        url__startswith="https://sap.test:44300/sap/opu/odata/sap/SVC/ENT"
    ).mock(side_effect=_handler)

    out = _make_client().query("SVC", "ENT", select="Foo,Bar")
    assert out["d"]["results"][0]["PurchaseOrder"] == "1"
    assert calls["n"] == 2


@respx.mock
def test_query_does_not_retry_on_non_400():
    respx.get(
        url__startswith="https://sap.test:44300/sap/opu/odata/sap/SVC/ENT"
    ).mock(return_value=httpx.Response(403, text="no auth"))
    with pytest.raises(SapODataError):
        _make_client().query("SVC", "ENT", select="Foo")


# ------------------------------------------------------- timeouts / errors


@respx.mock
def test_network_timeout_raises_sap_timeout():
    from sap_mcp.sap_client import SapTimeout

    respx.get("https://sap.test:44300/sap/opu/odata/sap/X/Y").mock(
        side_effect=httpx.ReadTimeout("slow")
    )
    with pytest.raises(SapTimeout) as ei:
        _make_client().get("X/Y")
    assert "did not finish answering" in str(ei.value)


@respx.mock
def test_trickling_response_is_cut_off_at_the_deadline():
    """httpx's per-phase timeouts never fire on a slow trickle; ours must."""
    import time
    from sap_mcp.sap_client import SapTimeout

    def _slow_body():
        for _ in range(20):
            time.sleep(0.1)
            yield b" "

    respx.get("https://sap.test:44300/sap/opu/odata/sap/X/Y").mock(
        return_value=httpx.Response(200, content=_slow_body(),
                                    headers={"content-type": "application/json"})
    )
    started = time.monotonic()
    with pytest.raises(SapTimeout):
        _make_client(timeout_seconds=0.3).get("X/Y")
    assert time.monotonic() - started < 1.5


@respx.mock
def test_sap_error_text_is_lifted_into_the_message():
    respx.get("https://sap.test:44300/sap/opu/odata/sap/X/Y").mock(
        return_value=httpx.Response(
            400, json={"error": {"code": "X/1", "message": {"lang": "en", "value": "Field FOO unknown"}}}
        )
    )
    with pytest.raises(SapODataError) as ei:
        _make_client().get("X/Y")
    assert "Field FOO unknown" in str(ei.value)


@respx.mock
def test_query_all_stops_with_timeout_instead_of_partial_rows():
    from sap_mcp.sap_client import SapTimeout

    respx.get(url__startswith="https://sap.test:44300/sap/opu/odata/sap/S/E").mock(
        return_value=httpx.Response(200, json={"d": {"results": [{"a": 1}, {"a": 2}]}})
    )
    rows = []
    with pytest.raises(SapTimeout) as ei:
        for r in _make_client().query_all("S", "E", page_size=2, max_seconds=0):
            rows.append(r)
    assert len(rows) == 2  # first page read, second never requested
    assert "No partial result" in str(ei.value)


# ------------------------------------------------------- metadata cache


@respx.mock
def test_metadata_is_cached_on_disk_across_clients():
    route = _mock_metadata()
    _make_client().list_entity_sets("API_BUSINESS_PARTNER")
    # A brand-new client (e.g. after a restart) must not download again.
    out = _make_client().get_entity_schema("API_BUSINESS_PARTNER", "A_Supplier")
    assert out["keys"] == ["Supplier"]
    assert route.call_count == 1


@respx.mock
def test_metadata_refresh_bypasses_cache():
    route = _mock_metadata()
    c = _make_client()
    c.list_entity_sets("API_BUSINESS_PARTNER")
    c.list_entity_sets("API_BUSINESS_PARTNER", refresh=True)
    assert route.call_count == 2


@respx.mock
def test_metadata_cache_is_per_system(monkeypatch):
    route = _mock_metadata()
    _make_client().list_entity_sets("API_BUSINESS_PARTNER")
    other = SapConfig(base_url="https://sap.test:44300", client="200", username="u",
                      password="p", verify_tls=False)
    SapODataClient(other, http_client=httpx.Client(verify=False)).list_entity_sets(
        "API_BUSINESS_PARTNER"
    )
    assert route.call_count == 2
