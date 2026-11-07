"""End-to-end test of the MCP tools, with the SAP backend mocked via respx.

We don't spin up the stdio transport — we call the underlying tool functions
that FastMCP registers. This proves the same code path Claude Desktop will
hit returns sensible JSON.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest
import respx

def _call(tool, **kwargs):
    fn = getattr(tool, "fn", tool)
    return fn(**kwargs)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SAP_BASE_URL", "https://sap.test:44300")
    monkeypatch.setenv("SAP_CLIENT", "100")
    monkeypatch.setenv("SAP_USERNAME", "SVC_CLAUDE")
    monkeypatch.setenv("SAP_PASSWORD", "pw")
    monkeypatch.setenv("SAP_VERIFY_TLS", "false")
    # Force a fresh client per test so env changes take effect, and zero the
    # session budget so tests don't inherit each other's spend.
    import sap_mcp.server as srv
    srv._client = None
    srv._spent_chars = 0
    srv._call_count = 0
    yield
    srv._client = None
    srv._spent_chars = 0
    srv._call_count = 0


def test_whoami_does_not_hit_sap():
    from sap_mcp.server import whoami
    out = json.loads(_call(whoami))
    assert out["base_url"] == "https://sap.test:44300"
    assert out["client"] == "100"
    assert out["username"] == "SVC_CLAUDE"
    # password must not leak
    assert "password" not in out
    assert "pw" not in json.dumps(out)


@respx.mock
def test_list_services_returns_json_string():
    respx.get(
        "https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"d": {"results": [{"Title": "API_BUSINESS_PARTNER"}, {"Title": "API_PRODUCT_SRV"}]}},
        )
    )
    from sap_mcp.server import list_services
    raw = _call(list_services)
    data = json.loads(raw)
    titles = [s["title"] for s in data["services"]]
    assert "API_BUSINESS_PARTNER" in titles
    assert data["total_matched"] == 2
    # The __metadata envelope must not survive into the response.
    assert "__metadata" not in raw


@respx.mock
def test_vendor_master_filters_and_returns_results():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_Supplier"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"d": {"results": [{"Supplier": "100001", "BusinessPartnerName": "ACME Chem"}]}},
        )
    )
    from sap_mcp.server import vendor_master
    out = json.loads(_call(vendor_master, search="ACME", top=5))
    assert out["d"]["results"][0]["Supplier"] == "100001"
    params = route.calls.last.request.url.params
    # BusinessPartnerName does not exist on A_SupplierType — searching it
    # returned a 400 on every call against the live tenant.
    assert "substringof('ACME',SupplierFullName)" in params["$filter"]
    assert "BusinessPartnerName" not in params["$filter"]
    assert params["$top"] == "5"
    assert "SupplierFullName" in params["$select"]


@respx.mock
def test_vendor_master_country_filter_explains_itself():
    """A_Supplier has no Country field here; say so instead of 400-ing."""
    from sap_mcp.server import vendor_master
    out = json.loads(_call(vendor_master, search="ACME", country="IN"))
    assert out["error"] == "unsupported_filter"
    assert "alternative" in out


@respx.mock
def test_gl_line_items_builds_date_filter():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": []}}))
    from sap_mcp.server import gl_line_items
    out = json.loads(
        _call(gl_line_items, 
            company_code="1000",
            fiscal_year="2026",
            posting_date_from="2026-04-01",
            posting_date_to="2026-04-30",
            gl_account="400000",
            top=25,
        )
    )
    assert out == {"d": {"results": []}}
    f = route.calls.last.request.url.params["$filter"]
    assert "CompanyCode eq '1000'" in f
    assert "FiscalYear eq '2026'" in f
    assert "PostingDate ge datetime'2026-04-01T00:00:00'" in f
    assert "PostingDate le datetime'2026-04-30T23:59:59'" in f
    assert "GLAccount eq '400000'" in f


@respx.mock
def test_sap_error_is_returned_as_json_not_raised():
    respx.get(
        "https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
    ).mock(return_value=httpx.Response(401, text="auth failed"))
    from sap_mcp.server import list_services
    out = json.loads(_call(list_services))
    assert out["error"] == "sap"
    assert out["status"] == 401


@respx.mock
def test_gl_account_balance_sums_signed_amounts():
    # Two pages worth of data. Our paging stops when a page returns fewer rows
    # than page_size, so a single page with <1000 rows is one round-trip.
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"GLAccount": "400000", "GLAccountName": "Sales",
         "DebitCreditCode": "S", "AmountInCompanyCodeCurrency": "1000.00",
         "CompanyCodeCurrency": "EUR"},
        {"GLAccount": "400000", "GLAccountName": "Sales",
         "DebitCreditCode": "H", "AmountInCompanyCodeCurrency": "-300.00",
         "CompanyCodeCurrency": "EUR"},
    ]}}))
    from sap_mcp.server import gl_account_balance
    out = json.loads(_call(gl_account_balance, 
        company_code="1000", gl_account="400000",
        as_of_date="2026-04-30", fiscal_year="2026",
    ))
    assert out["balance"] == 700.0
    assert out["debits_total"] == 1000.0
    assert out["credits_total"] == 300.0
    assert out["currency"] == "EUR"
    assert out["balance_side"] == "DR"
    assert out["truncated"] is False


@respx.mock
def test_trial_balance_groups_by_account():
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"GLAccount": "100000", "GLAccountName": "Cash",
         "DebitCreditCode": "S", "AmountInCompanyCodeCurrency": "5000.00",
         "CompanyCodeCurrency": "EUR", "IsBalanceSheetAccount": True},
        {"GLAccount": "100000", "GLAccountName": "Cash",
         "DebitCreditCode": "H", "AmountInCompanyCodeCurrency": "-1000.00",
         "CompanyCodeCurrency": "EUR", "IsBalanceSheetAccount": True},
        {"GLAccount": "400000", "GLAccountName": "Sales",
         "DebitCreditCode": "H", "AmountInCompanyCodeCurrency": "-2500.00",
         "CompanyCodeCurrency": "EUR", "IsBalanceSheetAccount": False},
    ]}}))
    from sap_mcp.server import trial_balance
    out = json.loads(_call(trial_balance, 
        company_code="1000", as_of_date="2026-04-30",
        gl_account_from="100000", gl_account_to="499999",
    ))
    assert out["account_count"] == 2
    by = {r["gl_account"]: r for r in out["rows"]}
    assert by["100000"]["balance"] == 4000.0
    assert by["400000"]["balance"] == -2500.0
    assert by["400000"]["balance_side"] == "CR"
    assert out["total_debits"] == 5000.0
    assert out["total_credits"] == 3500.0


def test_config_error_is_returned_as_json(monkeypatch):
    monkeypatch.delenv("SAP_BASE_URL", raising=False)
    # No address in the site settings either (a fresh install).
    from sap_mcp import environments as envs
    monkeypatch.setitem(envs.ENVIRONMENTS["prod"], "base_url", "")
    # Reset cached client so load_config runs again.
    import sap_mcp.server as srv
    srv._client = None
    from sap_mcp.server import list_services
    out = json.loads(_call(list_services))
    assert out["error"] == "config"
    assert "SAP_BASE_URL" in out["message"]


# ---------------------------------------------------------------------------
# P2P read tools (Session 5). Same respx-mocked style as above.
#
# Compatibility shim: depending on the installed FastMCP version, @mcp.tool()
# either returns a FunctionTool wrapper (exposes ``.fn``, AK's local mcp) or
# the bare function (newer mcp). ``_call`` handles both so the suite runs
# across versions.
# ---------------------------------------------------------------------------


@respx.mock
def test_purchase_orders_builds_filters_and_expand():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"PurchaseOrder": "4500000001", "Supplier": "100001", "CompanyCode": "1000"},
    ]}}))
    from sap_mcp.server import purchase_orders
    out = json.loads(_call(
        purchase_orders,
        company_code="1000", supplier="100001",
        po_date_from="2026-04-01", po_date_to="2026-05-31",
        include_items=True, top=10,
    ))
    assert out["d"]["results"][0]["PurchaseOrder"] == "4500000001"
    params = route.calls.last.request.url.params
    f = params["$filter"]
    assert "CompanyCode eq '1000'" in f
    assert "Supplier eq '100001'" in f
    assert "PurchaseOrderDate ge datetime'2026-04-01T00:00:00'" in f
    assert "PurchaseOrderDate le datetime'2026-05-31T23:59:59'" in f
    assert params["$expand"] == "to_PurchaseOrderItem"


@respx.mock
def test_purchase_requisitions_filters():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV/A_PurchaseRequisitionItem"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"PurchaseRequisition": "10000001", "Plant": "1100", "Material": "M1"},
    ]}}))
    from sap_mcp.server import purchase_requisitions
    out = json.loads(_call(
        purchase_requisitions,
        plant="1100", purchasing_group="P01",
        filter="ProcessingStatus eq '02'", top=20,
    ))
    assert out["d"]["results"][0]["Plant"] == "1100"
    f = route.calls.last.request.url.params["$filter"]
    assert "Plant eq '1100'" in f
    assert "PurchasingGroup eq 'P01'" in f
    assert "ProcessingStatus eq '02'" in f


@respx.mock
def test_material_documents_client_side_movement_type_filter():
    # One GR header with two items: a 101 (goods receipt) and a 122 (return).
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_MATERIAL_DOCUMENT_SRV/A_MaterialDocumentHeader"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"MaterialDocument": "5000000001", "MaterialDocumentYear": "2026",
         "PostingDate": "/Date(1780531200000)/",
         "to_MaterialDocumentItem": {"results": [
             {"Material": "M1", "Plant": "1100", "GoodsMovementType": "101", "PurchaseOrder": "4500000001"},
             {"Material": "M2", "Plant": "1100", "GoodsMovementType": "122", "PurchaseOrder": "4500000001"},
         ]}},
    ]}}))
    from sap_mcp.server import material_documents
    out = json.loads(_call(
        material_documents,
        posting_date_from="2026-06-01", posting_date_to="2026-06-09",
        goods_movement_type="101",
    ))
    assert out["document_count"] == 1
    items = out["documents"][0]["to_MaterialDocumentItem"]["results"]
    assert len(items) == 1
    assert items[0]["GoodsMovementType"] == "101"
    assert out["source_entity"].startswith("API_MATERIAL_DOCUMENT_SRV")


@respx.mock
def test_supplier_invoices_payment_blocked_filter():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV/A_SupplierInvoice"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"SupplierInvoice": "5105600001", "FiscalYear": "2026",
         "InvoicingParty": "100001", "PaymentBlockingReason": "A"},
    ]}}))
    from sap_mcp.server import supplier_invoices
    out = json.loads(_call(
        supplier_invoices,
        company_code="1000", invoicing_party="100001",
        payment_blocked_only=True, include_po_items=True,
    ))
    assert out["d"]["results"][0]["PaymentBlockingReason"] == "A"
    params = route.calls.last.request.url.params
    f = params["$filter"]
    assert "CompanyCode eq '1000'" in f
    assert "InvoicingParty eq '100001'" in f
    assert "PaymentBlockingReason ne ''" in f
    assert params["$expand"] == "to_SuplrInvcItemPurOrdRef"


@respx.mock
def test_ap_open_items_buckets_by_due_date():
    # as_of fixed to 2026-06-09. Due dates chosen to land in each bucket.
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"Supplier": "S1", "AmountInCompanyCodeCurrency": "100.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-06-04"},   # overdue (-5d)
        {"Supplier": "S1", "AmountInCompanyCodeCurrency": "200.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-06-14"},   # due_0_10 (+5d)
        {"Supplier": "S2", "AmountInCompanyCodeCurrency": "300.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-06-21"},   # due_11_15 (+12d)
        {"Supplier": "S2", "AmountInCompanyCodeCurrency": "400.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-07-20"},   # beyond (+41d)
    ]}}))
    from sap_mcp.server import ap_open_items
    out = json.loads(_call(
        ap_open_items, company_code="1000", as_of_date="2026-06-09", horizon_days=15,
    ))
    tb = out["totals_by_bucket"]
    assert tb["overdue"] == 100.0
    assert tb["due_0_10"] == 200.0
    assert tb["due_11_15"] == 300.0
    assert tb["beyond"] == 400.0
    assert out["open_total"] == 1000.0
    assert out["supplier_count"] == 2
    by = {r["supplier"]: r for r in out["rows"]}
    assert by["S1"]["overdue"] == 100.0 and by["S1"]["due_0_10"] == 200.0
    assert by["S2"]["due_11_15"] == 300.0 and by["S2"]["beyond"] == 400.0
# ---------------------------------------------------------------------------
# SD read tools (Session 6). Same respx-mocked style as the P2P tools above.
# ---------------------------------------------------------------------------


@respx.mock
def test_sales_orders_builds_filters_and_expand():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_SALES_ORDER_SRV/A_SalesOrder"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"SalesOrder": "1000001", "SoldToParty": "CUST01", "SalesOrganization": "1710"},
    ]}}))
    from sap_mcp.server import sales_orders
    out = json.loads(_call(
        sales_orders,
        sales_organization="1710", sold_to_party="CUST01",
        sales_order_type="OR",
        so_date_from="2026-04-01", so_date_to="2026-05-31",
        include_items=True, top=10,
    ))
    assert out["d"]["results"][0]["SalesOrder"] == "1000001"
    params = route.calls.last.request.url.params
    f = params["$filter"]
    assert "SalesOrganization eq '1710'" in f
    assert "SoldToParty eq 'CUST01'" in f
    assert "SalesOrderType eq 'OR'" in f
    assert "SalesOrderDate ge datetime'2026-04-01T00:00:00'" in f
    assert "SalesOrderDate le datetime'2026-05-31T23:59:59'" in f
    assert params["$expand"] == "to_Item"


@respx.mock
def test_outbound_deliveries_filters_and_not_yet_goods_issued():
    route = respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_OUTBOUND_DELIVERY_SRV;v=2/A_OutbDeliveryHeader"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"DeliveryDocument": "80000001", "ShippingPoint": "1710", "SoldToParty": "CUST01"},
    ]}}))
    from sap_mcp.server import outbound_deliveries
    out = json.loads(_call(
        outbound_deliveries,
        shipping_point="1710",
        planned_gi_date_from="2026-06-01", planned_gi_date_to="2026-06-09",
        not_yet_goods_issued=True, include_items=True, top=20,
    ))
    assert out["d"]["results"][0]["DeliveryDocument"] == "80000001"
    params = route.calls.last.request.url.params
    f = params["$filter"]
    assert "ShippingPoint eq '1710'" in f
    assert "PlannedGoodsIssueDate ge datetime'2026-06-01T00:00:00'" in f
    assert "PlannedGoodsIssueDate le datetime'2026-06-09T23:59:59'" in f
    assert "ActualGoodsMovementDate eq null" in f
    assert params["$expand"] == "to_DeliveryDocumentItem"


@respx.mock
def test_ar_open_items_buckets_by_due_date():
    # as_of fixed to 2026-06-09. Due dates chosen to land in each bucket.
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"Customer": "C1", "AmountInCompanyCodeCurrency": "1000.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-02-01"},
        {"Customer": "C1", "AmountInCompanyCodeCurrency": "200.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-06-01"},
        {"Customer": "C2", "AmountInCompanyCodeCurrency": "300.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-04-25"},
        {"Customer": "C2", "AmountInCompanyCodeCurrency": "400.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-06-20"},
        {"Customer": "C2", "AmountInCompanyCodeCurrency": "500.00",
         "CompanyCodeCurrency": "EUR", "NetDueDate": "2026-08-01"},
    ]}}))
    from sap_mcp.server import ar_open_items
    out = json.loads(_call(
        ar_open_items, company_code="1000", as_of_date="2026-06-09", horizon_days=30,
    ))
    tb = out["totals_by_bucket"]
    assert tb["overdue_90_plus"] == 1000.0
    assert tb["overdue_31_60"] == 300.0
    assert tb["overdue_1_30"] == 200.0
    assert tb["due_0_30"] == 400.0
    assert tb["beyond"] == 500.0
    assert tb["overdue_61_90"] == 0.0
    assert out["overdue_total"] == 1500.0
    assert out["open_total"] == 2400.0
    assert out["customer_count"] == 2
    by = {r["customer"]: r for r in out["rows"]}
    assert by["C1"]["overdue_90_plus"] == 1000.0 and by["C1"]["overdue_1_30"] == 200.0
    assert by["C1"]["overdue_total"] == 1200.0
    assert by["C2"]["overdue_31_60"] == 300.0 and by["C2"]["due_0_30"] == 400.0


# ---------------------------------------------------------------------------
# Internal / plant-operations read tools (Session 7). Same respx-mocked style.
# ---------------------------------------------------------------------------


@respx.mock
def test_material_stock_flags_below_reorder():
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"Material": "RM001", "Plant": "1000",
         "MatlWrhsStkQtyInMatlBaseUnit": "40", "MaterialBaseUnit": "KG"},
        {"Material": "RM001", "Plant": "1000",
         "MatlWrhsStkQtyInMatlBaseUnit": "20", "MaterialBaseUnit": "KG"},  # RM001 sums to 60
        {"Material": "SP010", "Plant": "1000",
         "MatlWrhsStkQtyInMatlBaseUnit": "5", "MaterialBaseUnit": "EA"},
    ]}}))
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductPlant"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"Material": "RM001", "Plant": "1000", "ReorderThresholdQuantity": "100"},  # 60<100 -> below, short 40
        {"Material": "SP010", "Plant": "1000", "ReorderThresholdQuantity": "3"},    # 5>3 -> ok
    ]}}))
    from sap_mcp.server import material_stock
    out = json.loads(_call(material_stock, plant="1000"))
    assert out["material_count"] == 2
    assert out["below_reorder_count"] == 1
    assert out["reorder_checked"] is True
    by = {r["material"]: r for r in out["rows"]}
    assert by["RM001"]["on_hand_qty"] == 60.0
    assert by["RM001"]["below_reorder"] is True
    assert by["RM001"]["shortfall"] == 40.0
    assert by["SP010"]["on_hand_qty"] == 5.0
    assert by["SP010"]["below_reorder"] is False
    # only_below_reorder narrows the rows
    out2 = json.loads(_call(material_stock, plant="1000", only_below_reorder=True))
    assert len(out2["rows"]) == 1 and out2["rows"][0]["material"] == "RM001"


@respx.mock
def test_material_stock_survives_missing_reorder_read():
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV/A_MatlStkInAcctMod"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"Material": "RM001", "Plant": "1000",
         "MatlWrhsStkQtyInMatlBaseUnit": "60", "MaterialBaseUnit": "KG"},
    ]}}))
    # MRP read returns an OData error -> on-hand still returned, no below flag
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_PRODUCT_SRV/A_ProductPlant"
    ).mock(return_value=httpx.Response(404, text="not found"))
    from sap_mcp.server import material_stock
    out = json.loads(_call(material_stock, plant="1000"))
    assert out["material_count"] == 1
    assert out["reorder_checked"] is False
    assert "below_reorder" not in out["rows"][0]


@respx.mock
def test_maintenance_orders_buckets_against_schedule():
    # as_of fixed to 2026-06-09, horizon 14 days.
    respx.get(
        "https://sap.test:44300/sap/opu/odata/sap/API_MAINTENANCEORDER/MaintenanceOrder"
    ).mock(return_value=httpx.Response(200, json={"d": {"results": [
        {"MaintenanceOrder": "OV1", "MaintenanceOrderType": "PM01",
         "MaintenanceOrderBasicEndDate": "2026-06-01",
         "MaintenanceProcessingPhase": "INProcess"},                 # overdue (-8)
        {"MaintenanceOrder": "DS1", "MaintenanceOrderType": "PM01",
         "MaintenanceOrderBasicEndDate": "2026-06-15"},              # due_soon (+6)
        {"MaintenanceOrder": "SC1", "MaintenanceOrderType": "PM02",
         "MaintenanceOrderBasicEndDate": "2026-07-30"},              # scheduled (+51)
        {"MaintenanceOrder": "DN1", "MaintenanceOrderType": "PM01",
         "MaintenanceOrderBasicEndDate": "2026-06-01",
         "MaintenanceProcessingPhase": "CLSD"},                      # excluded by only_open
    ]}}))
    from sap_mcp.server import maintenance_orders
    out = json.loads(_call(
        maintenance_orders, plant="1000", as_of_date="2026-06-09", horizon_days=14,
    ))
    tb = out["totals_by_bucket"]
    assert tb["overdue"] == 1
    assert tb["due_soon"] == 1
    assert tb["scheduled"] == 1
    assert out["order_count"] == 3  # DN1 (closed) excluded
    assert out["rows"][0]["bucket"] == "overdue"  # overdue sorted first


# ----------------------------------------- catalog search / pagination (0.2)

def _fat_catalog(n=300):
    """A catalog response shaped like the real one: every field present, plus
    the OData __metadata envelope SAP attaches to each entry."""
    return {"d": {"results": [
        {
            "__metadata": {
                "id": f"https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection('ZSVC_{i:04d}_0001')",
                "uri": f"https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection('ZSVC_{i:04d}_0001')",
                "type": "CATALOGSERVICE.Service",
            },
            "ID": f"ZSVC_{i:04d}_0001",
            "Title": ("Business Partner Master" if i % 10 == 0 else f"Service {i}"),
            "Author": "SAP",
            "Description": "Long description text that nobody needs " * 3,
            "TechnicalServiceName": f"ZSVC_{i:04d}",
            "TechnicalServiceVersion": "1",
            "MetadataUrl": f"https://sap.test:44300/sap/opu/odata/sap/ZSVC_{i:04d}/$metadata",
            "ServiceUrl": f"https://sap.test:44300/sap/opu/odata/sap/ZSVC_{i:04d}",
            "ImageUrl": "",
            "IsSapService": True,
            "UpdatedDate": "/Date(1700000000000)/",
        }
        for i in range(n)
    ]}}


@respx.mock
def test_list_services_search_filters_and_pages():
    respx.get(
        "https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
    ).mock(return_value=httpx.Response(200, json=_fat_catalog()))
    from sap_mcp.server import list_services

    out = json.loads(_call(list_services, search="business partner"))
    assert out["total_in_catalog"] == 300
    assert out["total_matched"] == 30
    assert out["returned"] == 30
    assert all("Business Partner" in s["title"] for s in out["services"])

    page = json.loads(_call(list_services, limit=10))
    assert page["returned"] == 10
    assert page["more_available"] is True
    page2 = json.loads(_call(list_services, limit=10, offset=10))
    assert page2["services"][0]["service_id"] != page["services"][0]["service_id"]


@respx.mock
def test_list_services_payload_is_dramatically_smaller():
    payload = _fat_catalog()
    respx.get(
        "https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
    ).mock(return_value=httpx.Response(200, json=payload))
    from sap_mcp.server import list_services

    before = len(json.dumps(payload))
    after = len(_call(list_services, search="business partner"))
    assert after < before / 20, f"expected >20x reduction, got {before}->{after}"


@respx.mock
def test_list_services_no_match_explains_itself():
    respx.get(
        "https://sap.test:44300/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/ServiceCollection"
    ).mock(return_value=httpx.Response(200, json=_fat_catalog(10)))
    from sap_mcp.server import list_services
    out = json.loads(_call(list_services, search="zzzz_nothing"))
    assert out["total_matched"] == 0
    assert "hint" in out


# ------------------------------------ payload slimming (Session 6 / token fix)

_PO_URL = ("https://sap.test:44300/sap/opu/odata/sap/"
           "API_PURCHASEORDER_PROCESS_SRV/A_PurchaseOrder")


def _po_payload(n_po=2, n_item=2, filler=0):
    def item(po, i):
        d = {
            "__metadata": {"uri": f"{_PO_URL}Item('{po}','{i}')", "type": "X"},
            "PurchaseOrderItem": f"{i:05d}", "Material": "MAT-1",
            "OrderQuantity": "10.000", "NetAmount": "1000.00",
            "to_ScheduleLine": {"__deferred": {"uri": f"{_PO_URL}/sched"}},
        }
        d.update({f"Filler{j}": "x" * 80 for j in range(filler)})
        return d

    return {"d": {"results": [
        {
            "__metadata": {"uri": f"{_PO_URL}('45{i:08d}')", "type": "X"},
            "PurchaseOrder": f"45{i:08d}", "Supplier": "2002497",
            "PurchaseOrderNetAmount": "12500.00",
            "to_Partner": {"__deferred": {"uri": f"{_PO_URL}/partner"}},
            "to_PurchaseOrderItem": {"results": [item(i, k) for k in range(n_item)]},
        }
        for i in range(n_po)
    ]}}


@respx.mock
def test_purchase_orders_strips_metadata_and_deferred():
    respx.get(url__startswith=_PO_URL).mock(
        return_value=httpx.Response(200, json=_po_payload()))
    from sap_mcp.server import purchase_orders
    raw = _call(purchase_orders, supplier="2002497", include_items=True)

    assert "__metadata" not in raw
    assert "__deferred" not in raw
    # Real data must survive.
    row = json.loads(raw)["d"]["results"][0]
    assert row["Supplier"] == "2002497"
    assert row["to_PurchaseOrderItem"]["results"][0]["Material"] == "MAT-1"


@respx.mock
def test_purchase_orders_sends_default_select():
    route = respx.get(url__startswith=_PO_URL).mock(
        return_value=httpx.Response(200, json=_po_payload()))
    from sap_mcp.server import purchase_orders

    _call(purchase_orders, supplier="2002497")
    sel = route.calls.last.request.url.params["$select"]
    assert "PurchaseOrder" in sel and "Supplier" in sel
    assert "to_PurchaseOrderItem/" not in sel  # no items requested

    _call(purchase_orders, supplier="2002497", include_items=True)
    sel = route.calls.last.request.url.params["$select"]
    assert "to_PurchaseOrderItem/Material" in sel
    # NetAmount does not exist on A_PurchaseOrderItem in this release; line
    # value is derived from NetPriceAmount / NetPriceQuantity instead.
    assert "to_PurchaseOrderItem/NetPriceAmount" in sel
    assert "to_PurchaseOrderItem/NetPriceQuantity" in sel
    assert "NetAmount," not in sel


@respx.mock
def test_explicit_select_overrides_the_default():
    route = respx.get(url__startswith=_PO_URL).mock(
        return_value=httpx.Response(200, json=_po_payload()))
    from sap_mcp.server import purchase_orders
    _call(purchase_orders, supplier="2002497", select="PurchaseOrder")
    assert route.calls.last.request.url.params["$select"] == "PurchaseOrder"


@respx.mock
def test_large_result_is_saved_to_excel_not_sent():
    # ~150 filler fields x 80 chars x items x headers: far too big for the chat.
    respx.get(url__startswith=_PO_URL).mock(
        return_value=httpx.Response(200, json=_po_payload(n_po=40, n_item=6, filler=60)))
    from sap_mcp.server import purchase_orders
    import sap_mcp.usage as usage
    raw = _call(purchase_orders, supplier="2002497", include_items=True)
    out = json.loads(raw)

    assert "error" not in out
    assert out["row_count"] == 40
    assert os.path.exists(out["saved_to"]) and out["saved_to"].endswith(".xlsx")
    assert 1 <= len(out["preview"]) <= 10
    assert len(raw) <= usage.INLINE_CHARS


# ------------------------------------------------------- session token budget

@respx.mock
def test_allowance_needs_the_users_click(monkeypatch):
    import sap_mcp.server as srv
    import sap_mcp.usage as usage
    respx.get(url__startswith=_PO_URL).mock(
        return_value=httpx.Response(200, json=_po_payload()))
    from sap_mcp.server import purchase_orders, token_budget

    # Allowance used up and the user says no (conftest default): nothing sent.
    srv._spent_chars = srv._SESSION_BUDGET_CHARS
    out = json.loads(_call(purchase_orders, supplier="2002497"))
    assert out["error"] == "budget_exhausted"
    assert not respx.calls
    # Claude cannot reset it by itself either.
    assert json.loads(_call(token_budget, reset=True))["reset"] is False

    # The user clicks Allow: the call goes through on a fresh allowance.
    asked = []
    monkeypatch.setattr(usage, "ask_to_continue", lambda state: asked.append(state) or True)
    ok = json.loads(_call(purchase_orders, supplier="2002497"))
    assert "d" in ok and len(asked) == 1
    assert srv._extensions == 1 and srv._call_count == 1


@respx.mock
def test_call_limit_also_asks(monkeypatch):
    import sap_mcp.server as srv
    import sap_mcp.usage as usage
    respx.get(url__startswith=_PO_URL).mock(
        return_value=httpx.Response(200, json=_po_payload()))
    from sap_mcp.server import purchase_orders
    srv._call_count = usage.MAX_CALLS
    out = json.loads(_call(purchase_orders, supplier="2002497"))
    assert out["error"] == "budget_exhausted" and not respx.calls


@respx.mock
def test_budget_warning_appears_near_the_ceiling():
    import sap_mcp.server as srv
    respx.get(url__startswith=_PO_URL).mock(
        return_value=httpx.Response(200, json=_po_payload()))
    from sap_mcp.server import purchase_orders

    srv._spent_chars = int(0.9 * srv._SESSION_BUDGET_CHARS)
    out = json.loads(_call(purchase_orders, supplier="2002497"))
    assert "_budget_warning" in out
    # The real data still comes back — a warning, not a refusal.
    assert out["data"]["d"]["results"][0]["Supplier"] == "2002497"


@respx.mock
def test_token_budget_reports_per_tool_and_logs_calls(tmp_path):
    respx.get(url__startswith=_PO_URL).mock(
        return_value=httpx.Response(200, json=_po_payload()))
    from sap_mcp.server import purchase_orders, token_budget
    from sap_mcp import credentials
    _call(purchase_orders, supplier="2002497")
    out = json.loads(_call(token_budget))
    assert out["calls"] == 1 and out["by_tool"]["purchase_orders"]["calls"] == 1
    log = (credentials.config_dir() / "usage_log.jsonl").read_text(encoding="utf-8").splitlines()
    entry = json.loads(log[-1])
    assert entry["tool"] == "purchase_orders" and entry["chars"] > 0


def test_core_toolset_hides_the_legacy_odata_tools():
    import anyio
    import sap_mcp.server as srv
    names = {t.name for t in anyio.run(srv.mcp.list_tools)}
    assert srv.TOOLSET == "core"
    assert "purchase_orders" not in names and "sales_ytd" not in names
    for keep in ("read_sap_table", "aggregate_sap_table", "run_sap_report", "query_entity",
                 "token_budget", "sap_login"):
        assert keep in names
