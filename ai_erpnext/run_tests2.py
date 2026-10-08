import frappe

def run_tests():
    from ai_erpnext.mapping.customer import match_customer
    from ai_erpnext.mapping.item import match_item
    from ai_erpnext.mapping.uom import match_uom
    from ai_erpnext.validation.totals import validate_totals
    from ai_erpnext.validation.duplicate import check_duplicate_sales_invoice
    from ai_erpnext.mapping.mapper import GlobalMapper
    from ai_erpnext.validation.validator import GlobalValidator

    print("Starting tests...")
    
    # Get a valid non-group customer group
    cg = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
    if not cg:
        cg = frappe.get_doc({"doctype": "Customer Group", "customer_group_name": "Test Non Group", "is_group": 0}).insert().name

    ig = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
    if not ig:
        ig = frappe.get_doc({"doctype": "Item Group", "item_group_name": "Test Non Group", "is_group": 0}).insert().name

    if not frappe.db.exists("Customer", "Test Exact Customer"):
        frappe.get_doc({"doctype": "Customer", "customer_name": "Test Exact Customer", "customer_group": cg}).insert(ignore_permissions=True)
        
    if not frappe.db.exists("Customer", "Test Ambiguous Customer A"):
        frappe.get_doc({"doctype": "Customer", "customer_name": "Test Ambiguous Customer A", "customer_group": cg}).insert(ignore_permissions=True)
    if not frappe.db.exists("Customer", "Test Ambiguous Customer B"):
        frappe.get_doc({"doctype": "Customer", "customer_name": "Test Ambiguous Customer B", "customer_group": cg}).insert(ignore_permissions=True)
        
    if not frappe.db.exists("Item", "Test Exact Item"):
        frappe.get_doc({"doctype": "Item", "item_code": "Test Exact Item", "item_name": "Test Exact Item", "item_group": ig}).insert(ignore_permissions=True)

    if not frappe.db.exists("Item", "Test Ambiguous Item A"):
        frappe.get_doc({"doctype": "Item", "item_code": "Test Ambiguous Item A", "item_name": "Test Ambiguous Item A", "item_group": ig}).insert(ignore_permissions=True)
    if not frappe.db.exists("Item", "Test Ambiguous Item B"):
        frappe.get_doc({"doctype": "Item", "item_code": "Test Ambiguous Item B", "item_name": "Test Ambiguous Item B", "item_group": ig}).insert(ignore_permissions=True)

    if not frappe.db.exists("UOM", "Test UOM"):
        frappe.get_doc({"doctype": "UOM", "uom_name": "Test UOM"}).insert(ignore_permissions=True)

    frappe.db.commit()

    res = match_customer("Test Exact Customer")
    assert res["matched"] == True and res["match_type"] == "exact", "1. Exact customer match failed"

    res = match_customer("test exact customer")
    assert res["matched"] == True and res["match_type"] in ["case-insensitive", "id_match"], "2. Case-insensitive customer match failed"

    res = match_customer("Unknown Customer XYZ 123")
    assert res["matched"] == False and res["match_type"] == "not_found", "3. Customer not found failed"

    res = match_customer("Test Ambiguous Customer")
    assert res["matched"] == False and res["match_type"] == "ambiguous" and len(res["candidates"]) >= 2, "4. Ambiguous customer failed"

    res = match_item({"item_name": "Test Exact Item"})
    assert res["matched"] == True and res["match_type"] == "exact_name", "5. Exact item match failed"

    res = match_item({"item_name": "Unknown Item XYZ 123"})
    assert res["matched"] == False and res["match_type"] == "not_found", "6. Item not found failed"

    res = match_item({"item_name": "Test Ambiguous Item"})
    assert res["matched"] == False and res["match_type"] == "ambiguous" and len(res["candidates"]) >= 2, "7. Ambiguous item failed"

    res = match_uom("Test UOM")
    assert res["matched"] == True and res["match_type"] == "exact", "8. UOM match failed"

    res = match_uom("Unknown UOM XYZ")
    assert res["matched"] == False and res["match_type"] == "not_found", "9. UOM not found failed"

    items = [{"qty": 0, "rate": 10, "amount": 0}, {"qty": 1, "rate": -5, "amount": -5}, {"qty": 2, "rate": 10, "amount": 100}]
    errors = validate_totals(items)
    assert len(errors) == 3, "10/11/12. Validation failed"

    dup_res = check_duplicate_sales_invoice({"customer": "Test Exact Customer"}, {"invoice_number": "NON_EXISTENT_INV_123"})
    assert dup_res == False, "13. Duplicate check failed"

    extracted = {
        "document_type": "Sales Invoice",
        "customer_name": "Test Exact Customer",
        "items": [
            {"item_name": "Test Exact Item", "qty": 1, "rate": 100, "amount": 100, "uom": "Test UOM"}
        ]
    }
    mapper = GlobalMapper()
    mapped, map_results = mapper.map_document(extracted)
    assert mapped["customer"] == frappe.db.get_value("Customer", {"customer_name": "Test Exact Customer"}, "name"), "14. Sales invoice mapping failed"
    
    extracted_missing_cust = {
        "document_type": "Sales Invoice",
        "customer_name": "Unknown Customer XYZ 123",
        "items": [
            {"item_name": "Test Exact Item", "qty": 1, "rate": 100, "amount": 100, "uom": "Test UOM"}
        ]
    }
    mapped_no_cust, map_results_no_cust = mapper.map_document(extracted_missing_cust)
    validator = GlobalValidator()
    errs, req_rev, is_dup = validator.validate_document("Sales Invoice", mapped_no_cust, map_results_no_cust, extracted_missing_cust)
    assert req_rev == True and any("Customer" in e for e in errs), "15. Missing customer validation failed"
    
    extracted_missing_item = {
        "document_type": "Sales Invoice",
        "customer_name": "Test Exact Customer",
        "items": [
            {"item_name": "Unknown Item XYZ 123", "qty": 1, "rate": 100, "amount": 100, "uom": "Test UOM"}
        ]
    }
    mapped_no_item, map_results_no_item = mapper.map_document(extracted_missing_item)
    errs_item, req_rev_item, is_dup_item = validator.validate_document("Sales Invoice", mapped_no_item, map_results_no_item, extracted_missing_item)
    assert req_rev_item == True and any("Item Code" in e for e in errs_item), "16. Missing item validation failed"
    
    invalid_extracted = {"document_type": "Invalid DocType 123"}
    mapped_inv, map_res_inv = mapper.map_document(invalid_extracted)
    assert "error" in map_res_inv, "17. Invalid doctype failed"

    print("All tests passed successfully.")

if __name__ == "__main__":
    frappe.connect("ai-erpnext.local")
    run_tests()
