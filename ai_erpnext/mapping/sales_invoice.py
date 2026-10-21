"""Kept for imports from before every document type shared one mapper (mapping/transaction.py)."""

from ai_erpnext.mapping.transaction import (
	BaseDocumentMapper,
	TaxTemplateMatcher,
	TransactionMapper,
)

SalesInvoiceMapper = TransactionMapper
