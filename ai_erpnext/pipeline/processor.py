import typing

class DocumentProcessor:
    def __init__(self, provider):
        self.provider = provider
    
    def process_file(self, file_content: bytes, file_type: str) -> dict:
        prompt = (
            "Extract structured data from this document. "
            "Determine the document type (e.g., 'Purchase Invoice', 'Sales Invoice') "
            "and extract fields such as supplier_name, customer_name, total_amount, "
            "items (with item_name, qty, rate, amount), date, invoice_number, etc. "
            "Return ONLY valid JSON."
        )
        return self.provider.extract_document_data(file_content, file_type, prompt)
