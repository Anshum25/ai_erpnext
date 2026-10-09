import hashlib
import typing

# Prompt used before the Groq migration (kept for rollback / reference).
LEGACY_PROMPT = "Extract structured data from this document. Determine the document type (e.g., 'Purchase Invoice', 'Sales Invoice') and extract fields such as supplier_name, customer_name, total_amount, items (with item_name, qty, rate, amount), date, invoice_number, etc. Return ONLY valid JSON."

# Prompt B2: the exact text benchmarked with Groq qwen/qwen3.8-27b on 2026-10-09
# (3 Sales Invoice layouts x 2 runs: 120/120 fields, mapping 6/6, validation 6/6).
# Field names are the ones the SalesInvoiceAdapter and the legacy mapper already read.
# Do not edit without re-running the benchmark; tests pin its SHA-256.
EXTRACTION_PROMPT = 'Extract structured data from this business document image.\nReturn ONE JSON object only: not a list, no markdown, no explanation.\n\nUse exactly these keys. Use null when a value is not printed or not readable.\n{\n  "document_type": one of "Sales Invoice", "Purchase Invoice", "Purchase Order", "Sales Order", "Quotation", "Delivery Note",\n  "supplier_name": seller / issuer name,\n  "customer_name": buyer / bill-to name,\n  "invoice_number": the document number exactly as printed,\n  "date": document date as YYYY-MM-DD,\n  "due_date": payment due date as YYYY-MM-DD, or null if no due date is printed (never use a delivery or other date),\n  "currency": ISO 4217 code, only if a currency code or an unambiguous symbol such as the rupee sign is printed, else null,\n  "items": [\n    {\n      "item_code": code from a column or label explicitly named item code, SKU, product code or part number, else null,\n      "item_name": the item\'s first line / short name only,\n      "description": the item\'s additional description lines, else null,\n      "qty": number,\n      "uom": unit of measure exactly as printed next to the quantity, else null,\n      "rate": unit price as a number,\n      "amount": line total as a number\n    }\n  ],\n  "subtotal": number or null,\n  "discount_amount": number or null,\n  "taxes": [ {"name": tax name as printed (for example CGST, SGST, IGST, VAT), "rate": percentage number, "amount": number} ],\n  "tax_rate": the overall tax percentage only when a single tax rate applies to the whole document, else null,\n  "tax_amount": total tax amount, the sum of all printed tax lines, or null,\n  "total_amount": grand total as a number\n}\n\nRules:\n- document_type: decide from the document\'s contents, not from a printed heading. A document billing a customer for goods or services is "Sales Invoice" even when headed INVOICE, TAX INVOICE, BILL, CASH MEMO or DEBIT MEMO.\n- item_code: never use row or serial numbers, HSN or SAC codes, or any other number as an item code.\n- Taxes: list each printed tax component separately in "taxes". Never report one component\'s rate (for example CGST 9%) as the overall tax_rate.\n- Keep item_name and description separate. Never merge description lines into item_name.\n- Copy item wording exactly. Never invent customers, invoice lines, item codes, units, dates or values.\n- Write numbers as plain JSON numbers without currency symbols or thousands separators.\n- Never calculate missing values and never adjust values to make totals match.'

EXTRACTION_PROMPT_SHA256 = "2d7e2cbc95efe288be15a41aa63b96caa37fbf009e1a263e8437cf43cbab9a1f"


class DocumentProcessor:
    def __init__(self, provider):
        self.provider = provider

    def process_file(self, file_content: bytes, file_type: str) -> dict:
        return self.provider.extract_document_data(file_content, file_type, EXTRACTION_PROMPT)
