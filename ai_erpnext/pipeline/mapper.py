import frappe
import typing

class ERPNextMapper:
    def map_document(self, extracted_data: dict) -> dict:
        """
        Maps extracted JSON data to ERPNext fields and matches masters.
        """
        doc_type = extracted_data.get('document_type')
        if not doc_type:
            frappe.throw("Could not determine document type.")
            
        mapped_data = {
            "doctype": doc_type,
            "items": []
        }
        
        # Match Supplier/Customer
        if doc_type in ['Purchase Invoice', 'Purchase Order']:
            supplier_name = extracted_data.get('supplier_name')
            mapped_data['supplier'] = self.match_supplier(supplier_name)
        elif doc_type in ['Sales Invoice', 'Sales Order']:
            customer_name = extracted_data.get('customer_name')
            mapped_data['customer'] = self.match_customer(customer_name)
            
        # Add basic fields
        if 'date' in extracted_data:
            if doc_type in ['Purchase Invoice', 'Sales Invoice']:
                mapped_data['posting_date'] = extracted_data['date']
            elif doc_type in ['Purchase Order', 'Sales Order']:
                mapped_data['transaction_date'] = extracted_data['date']
                
        # Map items
        for item in extracted_data.get('items', []):
            item_code = self.match_item(item.get('item_name'))
            mapped_item = {
                "item_code": item_code,
                "qty": item.get('qty', 1),
                "rate": item.get('rate', 0),
            }
            mapped_data['items'].append(mapped_item)
            
        return mapped_data

    def match_supplier(self, supplier_name: str) -> str:
        if not supplier_name:
            return ""
        # TODO: Implement fuzzy matching or LLM-based matching for supplier
        # For now, return exact match or empty
        supplier = frappe.db.get_value("Supplier", {"supplier_name": supplier_name}, "name")
        return supplier or ""

    def match_customer(self, customer_name: str) -> str:
        if not customer_name:
            return ""
        customer = frappe.db.get_value("Customer", {"customer_name": customer_name}, "name")
        return customer or ""

    def match_item(self, item_name: str) -> str:
        if not item_name:
            return ""
        item = frappe.db.get_value("Item", {"item_name": item_name}, "name")
        return item or ""
