"""Commercial direction of an uploaded business document: which ERPNext document, if any, it becomes for us.

A printed title is never enough. "Purchase Order" on a customer's letterhead, addressed to us, is a Sales Order in our
ERPNext; the same title on our letterhead, addressed to a supplier, is our Purchase Order. So the target DocType follows
from three things, all checked on the server:

1. the *kind* of document (an invoice, an order, a delivery document ...), from the extracted title (perspective-free);
2. which party is the seller and which the buyer, from the extracted issuer/recipient and seller/buyer names, which
   must agree with the role the kind of document gives its issuer (an invoice is issued by the seller, a purchase
   order by the buyer ...);
3. which of those parties is *our* company: the company the user selected in this conversation, or the user's
   validated default company (never a company picked only because the model wrote its name).

The model only extracts text. It never decides the target: TARGETS below is the explicit allowlist, and anything that is
contradictory, unknown or not in the allowlist is returned as a status that the caller must turn into a question or a
refusal. Nothing here reads or writes business records except the user's own permitted Company names.
"""

import re
from dataclasses import asdict, dataclass, field

import frappe

from ai_erpnext.constants import DOCUMENT_TYPE_CONFIDENCE_THRESHOLD
from ai_erpnext.mapping.normalize import clean_text, normalize_name, parse_number

SELLER, BUYER, INTERNAL = "seller", "buyer", "internal"

# Perspective-free kinds of documents -> the role their issuer has in the transaction.
ISSUER_ROLE = {
	"invoice": SELLER,  # a bill is issued by whoever supplied the goods or services
	"credit_note": SELLER,  # the seller credits the buyer (returned goods, price reduction)
	"debit_note": BUYER,  # the buyer debits the seller for returned goods (ERPNext's "Debit Note")
	"purchase_order": BUYER,
	"sales_order": SELLER,  # an order confirmation / acknowledgement
	"quotation": SELLER,
	"delivery_note": SELLER,  # the deliverer's delivery document / challan
	"goods_receipt": BUYER,  # the receiver's goods received note
	"material_request": INTERNAL,
	"bom": INTERNAL,
}
KIND_LABELS = {"invoice": "Invoice", "credit_note": "Credit Note", "debit_note": "Debit Note", "purchase_order": "Purchase Order",
	"sales_order": "Sales Order / Order Confirmation", "quotation": "Quotation", "delivery_note": "Delivery Note",
	"goods_receipt": "Goods Receipt Note", "material_request": "Material Request", "bom": "Bill of Materials"}

# Printed titles (normalised) -> kind. "Sales Invoice" and "Purchase Invoice" are both just invoices: which one it is for us
# depends on who issued it. Titles that are not listed (Proforma Invoice, Packing List ...) are unknown, never guessed.
SOURCE_ALIASES = {
	"invoice": "invoice", "sales invoice": "invoice", "purchase invoice": "invoice", "tax invoice": "invoice", "bill": "invoice",
	"cash memo": "invoice", "commercial invoice": "invoice", "retail invoice": "invoice", "gst invoice": "invoice", "vat invoice": "invoice",
	"supplier invoice": "invoice", "vendor invoice": "invoice", "customer invoice": "invoice", "invoice sales": "invoice",
	"credit note": "credit_note", "credit memo": "credit_note", "sales return": "credit_note", "sales invoice return": "credit_note",
	"debit note": "debit_note", "debit memo": "debit_note", "purchase return": "debit_note", "purchase invoice return": "debit_note",
	"purchase order": "purchase_order", "po": "purchase_order",
	"sales order": "sales_order", "order confirmation": "sales_order", "order acknowledgement": "sales_order",
	"order acknowledgment": "sales_order", "sales order confirmation": "sales_order",
	"quotation": "quotation", "quote": "quotation", "estimate": "quotation", "sales quotation": "quotation",
	"supplier quotation": "quotation", "price quotation": "quotation",
	"delivery note": "delivery_note", "delivery challan": "delivery_note", "dispatch note": "delivery_note", "challan": "delivery_note",
	"goods receipt note": "goods_receipt", "goods received note": "goods_receipt", "grn": "goods_receipt",
	"purchase receipt": "goods_receipt", "receiving report": "goods_receipt",
	"material request": "material_request", "material requisition": "material_request", "purchase requisition": "material_request",
	"store requisition": "material_request", "indent": "material_request",
	"bom": "bom", "bill of materials": "bom", "bill of material": "bom",
}

# The explicit allowlist: (kind, our role) -> registry key of the document we create. None: recognised, but not something we
# may create (a supplier's quotation is a Supplier Quotation; a customer's goods receipt note is their record, not ours).
TARGETS = {
	("invoice", SELLER): "Sales Invoice", ("invoice", BUYER): "Purchase Invoice",
	("credit_note", SELLER): "Sales Invoice Return", ("credit_note", BUYER): "Purchase Invoice Return",
	("debit_note", BUYER): "Purchase Invoice Return", ("debit_note", SELLER): "Sales Invoice Return",
	("purchase_order", BUYER): "Purchase Order", ("purchase_order", SELLER): "Sales Order",
	("sales_order", SELLER): "Sales Order", ("sales_order", BUYER): "Purchase Order",
	("quotation", SELLER): "Quotation", ("quotation", BUYER): None,
	("delivery_note", SELLER): "Delivery Note", ("delivery_note", BUYER): "Purchase Receipt",
	("goods_receipt", BUYER): "Purchase Receipt", ("goods_receipt", SELLER): None,
	("material_request", INTERNAL): "Material Request", ("bom", INTERNAL): "BOM",
}
UNSUPPORTED_REASONS = {
	("quotation", BUYER): "a supplier's quotation to your company is a Supplier Quotation, which I can't create from an image",
	("goods_receipt", SELLER): "a customer's goods receipt note is their own record; your delivery is recorded as a Delivery Note",
}

# Statuses: only "ok" may lead to a document.
OK, UNKNOWN_TYPE, NEEDS_COMPANY, NEEDS_ROLE, CONFLICT, UNSUPPORTED = "ok", "unknown_type", "needs_company", "needs_role", "conflict", "unsupported"

# Legal-form words that don't distinguish two names of the *same* organisation ("AI Test Company Pvt. Ltd." = "AI Test Company").
_LEGAL_WORDS = {"pvt", "private", "ltd", "limited", "llc", "llp", "inc", "incorporated", "corp", "corporation", "co", "company", "plc",
	"gmbh", "ag", "sa", "bv", "pte", "pty", "the"}


@dataclass
class Classification:
	source_type: str | None = None  # the title as printed / extracted
	kind: str | None = None
	issuer: str | None = None
	recipient: str | None = None
	seller: str | None = None
	buyer: str | None = None
	company: str | None = None
	company_source: str | None = None  # "selected" (by the user), "document" (named on it and confirmed context), "default"
	company_candidates: list = field(default_factory=list)
	our_role: str | None = None
	role_source: str | None = None  # "document" or "selected"
	counterparty: str | None = None  # the other party's name as printed (resolved to a Customer/Supplier by the mapper)
	target_key: str | None = None
	target_doctype: str | None = None
	status: str = UNKNOWN_TYPE
	message: str | None = None
	confidence: float | None = None
	ask: str | None = None  # the question that can resolve a non-ok status: "company", "role" or None (cancel only)

	def to_dict(self):
		return asdict(self)

	@property
	def ok(self):
		return self.status == OK

	def describe(self):
		"""One plain sentence for the confirmation card."""
		parts = [KIND_LABELS.get(self.kind) or (self.source_type or "Document")]
		if self.issuer:
			parts.append(f"issued by {self.issuer}")
		if self.recipient:
			parts.append(f"to {self.recipient}")
		return " ".join(parts)


def source_kind(raw):
	"""Kind of a printed/extracted title, or None. Exact (normalised) titles only: no substring guessing."""
	text = normalize_name(raw)
	if not text:
		return None
	return SOURCE_ALIASES.get(text) or SOURCE_ALIASES.get(re.sub(r"\s+(document|copy|original|duplicate)$", "", text))


def _legal_free(text):
	return " ".join(w for w in normalize_name(text).split() if w not in _LEGAL_WORDS)


def company_rows(usable):
	"""(name, company_name, abbr) of the user's usable companies (names only; already permission-derived)."""
	if not usable:
		return []
	return frappe.get_all("Company", filters={"name": ["in", list(usable)]}, fields=["name", "company_name", "abbr"], limit=0)


def company_of(name, rows):
	"""The usable company `name` denotes, or None. Same organisation only: exact name, name without legal-form words, or the
	exact abbreviation. Fuzzy similarity is never enough to put our company on one side of a transaction."""
	text = clean_text(name, 200)
	if not text:
		return None
	wanted, wanted_core = normalize_name(text), _legal_free(text)
	hits = set()
	for r in rows:
		names = {normalize_name(r.name), normalize_name(r.company_name)}
		cores = {_legal_free(r.name), _legal_free(r.company_name)} - {""}
		if wanted in names or (wanted_core and wanted_core in cores) or (r.abbr and text.strip().lower() == r.abbr.lower()):
			hits.add(r.name)
	return hits.pop() if len(hits) == 1 else None


def _same(a, b):
	return bool(a and b) and (normalize_name(a) == normalize_name(b) or (_legal_free(a) and _legal_free(a) == _legal_free(b)))


def evidence(extracted):
	"""Party names and title from an extraction. Older extractions (Prompt B2) have no issuer/recipient keys: there
	supplier_name was defined as the seller and customer_name as the buyer."""
	data = extracted if isinstance(extracted, dict) else {}

	def pick(*keys):
		for k in keys:
			v = clean_text(data.get(k), 200)
			if v:
				return v
		return None

	return {
		"source_type": clean_text(data.get("document_type"), 140),
		"issuer": pick("issuer_name", "issued_by"),
		"recipient": pick("recipient_name", "addressed_to", "bill_to_name"),
		"seller": pick("seller_name", "supplier_name", "vendor_name"),
		"buyer": pick("buyer_name", "customer_name", "bill_to", "client_name"),
		"is_return": data.get("is_return") is True,
	}


def _confidence(extracted):
	for key in ("document_type_confidence", "confidence"):
		value = parse_number(extracted.get(key)) if isinstance(extracted, dict) else None
		if value is not None:
			return max(0.0, min(1.0, value / 100.0 if value > 1 else value))
	return None


def classify(extracted, usable_companies, default_company=None, selected_company=None, selected_role=None):
	"""Classification of one extraction for the current user.

	usable_companies: the companies the user may work with (already permission-derived). default_company: the user's
	validated default (must be usable). selected_company / selected_role: explicit answers given in this conversation."""
	ev = evidence(extracted)
	c = Classification(source_type=ev["source_type"], issuer=ev["issuer"], recipient=ev["recipient"], confidence=_confidence(extracted))
	kind = source_kind(ev["source_type"])
	if kind == "invoice" and ev["is_return"]:
		kind = "credit_note"  # an invoice flagged as a return; the issuer decides which side's return it is
	c.kind = kind
	if not kind:
		c.status = UNKNOWN_TYPE
		c.message = (f"I can't tell what kind of document “{ev['source_type']}” is" if ev["source_type"] else "I can't tell what kind of document this is") + "."
		return c
	if c.confidence is not None and c.confidence < DOCUMENT_TYPE_CONFIDENCE_THRESHOLD:
		c.status = UNKNOWN_TYPE
		c.message = "The document type could not be read with enough confidence."
		return c

	if kind == "debit_note" and not (ev["issuer"] or ev["recipient"]):
		# A buyer's debit note returns goods; a seller's "debit note/memo" charges more. Without knowing who issued it,
		# it is neither a return nor an invoice.
		c.status = UNSUPPORTED
		c.message = "I can't tell who issued this debit note, so I can't tell whether it returns goods or charges more."
		return c

	rows = company_rows(usable_companies)
	usable = {r.name for r in rows}
	issuer_role = ISSUER_ROLE[kind]

	# Seller and buyer: explicit names, checked against what the issuer's role implies.
	seller, buyer = ev["seller"], ev["buyer"]
	if issuer_role in (SELLER, BUYER) and (ev["issuer"] or ev["recipient"]):
		d_seller = ev["issuer"] if issuer_role == SELLER else ev["recipient"]
		d_buyer = ev["recipient"] if issuer_role == SELLER else ev["issuer"]
		swapped = (_same(seller, d_buyer) and not _same(seller, d_seller)) or (_same(buyer, d_seller) and not _same(buyer, d_buyer))
		if swapped:
			_company(c, rows, usable, default_company, selected_company, ev, final=False)
			# Two readings of the same document. Only an explicit answer from the user picks one: the reading that puts
			# our company on the side the user named. Without one (or if neither reading fits), nothing is created.
			readings = [(seller, buyer), (d_seller, d_buyer)]
			chosen = next((r for r in readings if c.company and selected_role in (SELLER, BUYER)
				and company_of(r[0 if selected_role == SELLER else 1], rows) == c.company), None)
			if not chosen:
				c.seller, c.buyer = seller, buyer
				c.status, c.ask = CONFLICT, "role"
				c.message = (f"The document's parties contradict each other: a {KIND_LABELS[kind].lower()} is issued by the "
					f"{issuer_role}, but “{ev['issuer'] or ev['recipient']}” is also named as the {'buyer' if issuer_role == SELLER else 'seller'}.")
				return c
			seller, buyer = chosen
		else:
			seller, buyer = seller or d_seller, buyer or d_buyer
	c.seller, c.buyer = seller, buyer

	_company(c, rows, usable, default_company, selected_company, ev)
	if c.status == NEEDS_COMPANY:
		return c

	if issuer_role == INTERNAL:
		outsider = next((n for n in (ev["issuer"], ev["seller"], ev["buyer"], ev["recipient"]) if n and company_of(n, rows) not in (None, c.company)), None)
		if outsider:
			c.status, c.ask = CONFLICT, "company"
			c.message = f"This {KIND_LABELS[kind].lower()} names “{outsider}”, another of your companies, so I can't tell which company it belongs to."
			return c
		c.our_role, c.role_source = INTERNAL, "document"
		return _target(c)

	seller_us = bool(seller) and company_of(seller, rows) == c.company
	buyer_us = bool(buyer) and company_of(buyer, rows) == c.company
	other_company = next((n for n in (seller, buyer) if n and company_of(n, rows) not in (None, c.company)), None)
	if seller_us and buyer_us:
		c.status = CONFLICT
		c.message = f"Both the seller and the buyer on this document are {c.company}, so I can't tell which side your company is on."
		return c
	if other_company and not (seller_us or buyer_us):
		c.status, c.ask = CONFLICT, "company"
		c.message = (f"This document is between “{seller or '?'}” and “{buyer or '?'}”, and one of them is another of your companies, "
			f"not {c.company}. Choose the company it belongs to.")
		return c
	document_role = SELLER if seller_us else BUYER if buyer_us else None
	if selected_role in (SELLER, BUYER):
		if document_role and selected_role != document_role:
			c.status, c.ask = CONFLICT, "role"
			c.message = (f"The document names {c.company} as the {document_role}, so I can't record it with your company as the "
				f"{selected_role}.")
			return c
		c.our_role, c.role_source = selected_role, "document" if document_role else "selected"
	elif document_role:
		c.our_role, c.role_source = document_role, "document"
	else:
		c.status, c.ask = NEEDS_ROLE, "role"
		c.message = (f"Neither the seller (“{seller or 'not printed'}”) nor the buyer (“{buyer or 'not printed'}”) on this "
			f"{KIND_LABELS[kind].lower()} is {c.company}.")
		return c
	# The other party may be missing from the document: the direction is still proven, and the mapper asks which
	# customer/supplier it is instead of guessing.
	c.counterparty = buyer if c.our_role == SELLER else seller
	counter_company = company_of(c.counterparty, rows) if c.counterparty else None
	if counter_company == c.company:
		c.status = CONFLICT
		c.message = f"The other party on this document is {c.company} itself."
		return c
	if counter_company and c.company_source != "selected":
		# Between two of the user's companies: whose books it belongs in (a sale for one, a purchase for the other) is the
		# user's decision.
		c.status, c.ask = CONFLICT, "company"
		c.message = (f"This document is between two of your companies ({c.company} and {counter_company}). Choose the company "
			"whose books it belongs in.")
		return c
	return _target(c)


def _company(c, rows, usable, default_company, selected_company, ev, final=True):
	"""Our company: the user's explicit choice, else the default (validated context). A non-default company named on the
	document is only a candidate the user must confirm."""
	named = [x for x in dict.fromkeys(company_of(n, rows) for n in (ev["seller"], ev["buyer"], ev["issuer"], ev["recipient"]) if n) if x]
	c.company_candidates = named
	if selected_company and selected_company in usable:
		c.company, c.company_source = selected_company, "selected"
	elif default_company and default_company in usable and (not named or default_company in named):
		c.company, c.company_source = default_company, "document" if default_company in named else "default"
	elif len(usable) == 1 and (not named or named == list(usable)):
		c.company, c.company_source = next(iter(usable)), "document" if named else "default"
	if not c.company and final:
		c.status, c.ask = NEEDS_COMPANY, "company"
		c.message = (f"The document names {', '.join(named)}, which isn't your default company. Which of your companies is it for?" if named
			else "Which of your companies is this document for?")
	return c


def _target(c):
	key = (c.kind, c.our_role)
	if key not in TARGETS:
		c.status = UNSUPPORTED
		c.message = "I can't create anything from this kind of document."
		return c
	target = TARGETS[key]
	if not target:
		c.status = UNSUPPORTED
		c.message = f"I can't create anything from this document: {UNSUPPORTED_REASONS[key]}."
		return c
	from ai_erpnext.registry.document_registry import get_definition

	definition = get_definition(target)
	if not definition:
		c.status = UNSUPPORTED
		c.message = f"Creating a {target} from an image isn't supported."
		return c
	c.target_key, c.target_doctype, c.status = definition.key, definition.target_doctype, OK
	return c
