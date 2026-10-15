"""What the assistant can explain, look up and create, per ERPNext module and DocType.

Look-ups come from chat.queries.REGISTRY and creation from chat.forms.CREATABLE, so this
never claims an action the chat can't perform. The short explanations are general ERPNext
guidance (standard ERPNext v16 behaviour), never data from the user's site; they are used:
- to tell the router model what the assistant can do (capability_summary), so its general
  answers don't promise unsupported actions;
- to add an accurate "what I can do here" line to how-to answers (footer);
- as the answer itself when no AI provider is available (answer).
"""

import re

GUIDE = {
	"Sales Invoice": {
		"module": "Accounts / Selling",
		"what": "A Sales Invoice is the bill you send a customer. When it is submitted, ERPNext posts the receivable and income to the "
			"general ledger, and its outstanding amount goes down as payments are made against it.",
		"create": ["Open Selling > Sales Invoice > New (or create it from a Sales Order or Delivery Note to copy the items).",
			"Choose the customer; company, currency, price list and payment terms come from your defaults.",
			"Add the items with quantity and rate, and pick a Sales Taxes and Charges Template if taxes apply.",
			"Check the posting and due dates, save it as a Draft, then Submit when it is final."],
		"after_submit": "Submitting posts the receivable and income to the general ledger (and stock, if Update Stock is ticked). The invoice "
			"can then be paid with a Payment Entry; to change it you cancel and amend it.",
	},
	"Purchase Invoice": {
		"module": "Accounts / Buying",
		"what": "A Purchase Invoice records a supplier's bill. When it is submitted, ERPNext posts the payable and the expense or stock-received "
			"value to the general ledger.",
		"create": ["Open Buying > Purchase Invoice > New (or create it from a Purchase Order or Purchase Receipt).",
			"Choose the supplier and enter the supplier's invoice number and date.",
			"Add the items with quantity and rate, and the purchase taxes if any.",
			"Save it as a Draft, then Submit it when it is final."],
		"after_submit": "Submitting posts the payable to the general ledger. You then pay it with a Payment Entry; changes need a cancel and amend.",
	},
	"Sales Order": {
		"module": "Selling",
		"what": "A Sales Order is a confirmed order from a customer. It reserves the plan for delivery and billing: Delivery Notes and Sales "
			"Invoices are created from it, and it tracks how much has been delivered and billed.",
		"create": ["Open Selling > Sales Order > New (or create it from a Quotation).",
			"Choose the customer and the delivery date.",
			"Add the items with quantity and rate, and the warehouse stock will be delivered from.",
			"Save and Submit it to confirm the order."],
		"after_submit": "A submitted Sales Order can be delivered (Delivery Note) and billed (Sales Invoice); it does not post to the ledger itself.",
	},
	"Purchase Order": {
		"module": "Buying",
		"what": "A Purchase Order is the order you send a supplier. Purchase Receipts and Purchase Invoices are created from it, and it tracks "
			"how much has been received and billed.",
		"create": ["Open Buying > Purchase Order > New (or create it from a Material Request or Supplier Quotation).",
			"Choose the supplier and the Required By date.",
			"Add the items with quantity and rate; stock items need a target warehouse (Set Target Warehouse or per row).",
			"Save and Submit it, then send it to the supplier."],
		"after_submit": "A submitted Purchase Order can be received (Purchase Receipt) and billed (Purchase Invoice); it does not post to the ledger itself.",
	},
	"Quotation": {
		"module": "Selling / CRM",
		"what": "A Quotation is a price offer to a customer or lead. When accepted, it is turned into a Sales Order.",
		"create": ["Open Selling > Quotation > New.", "Choose the customer (or lead), the items, quantities and rates, and a Valid Till date.",
			"Save and Submit it, then send it to the customer."],
		"after_submit": "A submitted Quotation can be converted into a Sales Order; it does not affect stock or accounts.",
	},
	"Delivery Note": {
		"module": "Stock / Selling",
		"what": "A Delivery Note records goods shipped to a customer. It reduces stock in the source warehouse and is usually billed with a "
			"Sales Invoice afterwards.",
		"create": ["Open Stock > Delivery Note > New (or create it from a Sales Order).", "Choose the customer, the items and quantities, and the "
			"warehouse stock leaves from.", "Save and Submit it when the goods are shipped."],
		"after_submit": "Submitting reduces stock in the source warehouse and posts the stock value; you then bill it with a Sales Invoice.",
	},
	"Purchase Receipt": {
		"module": "Stock / Buying",
		"what": "A Purchase Receipt records goods received from a supplier. It adds stock to the accepted warehouse; the supplier's bill is "
			"recorded separately as a Purchase Invoice.",
		"create": ["Open Stock > Purchase Receipt > New (or create it from a Purchase Order).", "Choose the supplier, the items, received quantities "
			"and the accepted warehouse.", "Save and Submit it when the goods are in."],
		"after_submit": "Submitting adds stock to the accepted warehouse and posts the stock-received value; you then record the bill as a Purchase Invoice.",
	},
	"Stock Entry": {
		"module": "Stock",
		"what": "A Stock Entry moves stock without a customer or supplier: material receipts and issues, transfers between warehouses, and "
			"manufacturing or repacking.",
		"create": ["Open Stock > Stock Entry > New.", "Choose the Stock Entry Type (Material Receipt, Issue, Transfer, ...).",
			"Add the items with quantities and source/target warehouses.", "Save and Submit it."],
		"after_submit": "Submitting updates stock levels in the warehouses involved and posts the stock value.",
	},
	"Payment Entry": {
		"module": "Accounts",
		"what": "A Payment Entry records money received from customers or paid to suppliers, and allocates it against their outstanding invoices.",
		"create": ["Open Accounts > Payment Entry > New (or use Create > Payment on an invoice).", "Choose Receive or Pay, the party, the amount and "
			"the bank or cash account.", "Allocate it to the invoices, then Save and Submit it."],
		"after_submit": "Submitting posts the payment to the ledger and reduces the outstanding amounts of the allocated invoices.",
	},
	"Customer": {"module": "Selling / CRM", "what": "A Customer is a party you sell to. Its group, territory, currency, contacts and addresses are "
		"used as defaults on sales documents.", "create": ["Open Selling > Customer > New.", "Enter the customer name and type, and choose the "
		"customer group and territory.", "Save it; add contacts and addresses from the customer form."]},
	"Supplier": {"module": "Buying", "what": "A Supplier is a party you buy from. Its group, currency, contacts and addresses are used as defaults "
		"on purchase documents.", "create": ["Open Buying > Supplier > New.", "Enter the supplier name, type and group.", "Save it; add contacts "
		"and addresses from the supplier form."]},
	"Item": {"module": "Stock", "what": "An Item is a product or service you buy or sell. Stock items are tracked by warehouse; non-stock items and "
		"services are not.", "create": ["Open Stock > Item > New.", "Enter the item code, name, item group and stock unit of measure.",
		"Tick Maintain Stock for stock items, then save it."]},
	"Warehouse": {"module": "Stock", "what": "A Warehouse is a location that holds stock for one company. Warehouses form a tree; only non-group "
		"warehouses hold stock.", "create": ["Open Stock > Warehouse > New.", "Enter the name, choose the company and the parent warehouse, then save it."]},
	"Employee": {"module": "HR", "what": "An Employee record holds a person's role in the company (department, designation, branch).",
		"create": ["Open HR > Employee > New.", "Enter the name, company, date of joining and other required details, then save it."]},
	"Contact": {"module": "CRM", "what": "A Contact is a person (name, email, phone) linked to customers, suppliers or other records.",
		"create": ["Open a Customer or Supplier and use Add Contact, or open Contact > New and link it."]},
}
MODULES = {
	"Selling": ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "Customer"),
	"Buying": ("Purchase Order", "Purchase Receipt", "Purchase Invoice", "Supplier"),
	"Stock": ("Item", "Warehouse", "Stock Entry", "Delivery Note", "Purchase Receipt"),
	"Accounts": ("Sales Invoice", "Purchase Invoice", "Payment Entry"),
	"HR": ("Employee",),
	"CRM": ("Customer", "Contact", "Quotation"),
}
MODULE_WHAT = {
	"Selling": "The Selling module covers the sales cycle: quotations, sales orders, deliveries and customer billing.",
	"Buying": "The Buying module covers purchasing: purchase orders, receipts and supplier bills.",
	"Stock": "The Stock module tracks items and their quantities and value per warehouse: goods received, delivered, transferred "
		"and adjusted, with stock ledgers and reports.",
	"Accounts": "The Accounts module is the general ledger: invoices, payments, journal entries, taxes and financial reports.",
	"HR": "The HR module manages employees and their organisational details.",
	"CRM": "The CRM module manages leads, opportunities, customers and their contacts.",
}
MODULE_ALIASES = {"inventory": "Stock", "stock": "Stock", "selling": "Selling", "sales": "Selling", "buying": "Buying", "purchase": "Buying",
	"purchasing": "Buying", "accounts": "Accounts", "accounting": "Accounts", "finance": "Accounts", "hr": "HR", "human resources": "HR", "crm": "CRM"}
COMPARE = {
	frozenset(("Purchase Receipt", "Purchase Invoice")): "A Purchase Receipt records the goods you received (it changes stock); a Purchase "
		"Invoice records the supplier's bill (it creates the payable). You usually make the receipt when goods arrive and the invoice when the bill comes.",
	frozenset(("Delivery Note", "Sales Invoice")): "A Delivery Note records goods shipped (it reduces stock); a Sales Invoice bills the customer "
		"(it creates the receivable). An invoice can also update stock itself if you tick Update Stock.",
	frozenset(("Sales Order", "Sales Invoice")): "A Sales Order is the customer's confirmed order (no accounting entries); the Sales Invoice is "
		"the bill that posts the receivable and income.",
	frozenset(("Quotation", "Sales Order")): "A Quotation is an offer the customer may still accept or decline; a Sales Order is the confirmed order.",
	frozenset(("Purchase Order", "Purchase Invoice")): "A Purchase Order is what you ordered from the supplier; the Purchase Invoice is their "
		"bill, which posts the payable.",
	frozenset(("Purchase Order", "Purchase Receipt")): "A Purchase Order is what you ordered; a Purchase Receipt records what actually arrived.",
}
TOPICS = [
	(re.compile(r"\b(check|see|view|find|track)\b.*\boutstanding\b.*\b(customer|receivable|payment)", re.I),
		"In ERPNext, open Accounts > Accounts Receivable (or the customer's dashboard) to see what each customer owes; overdue invoices are "
		"listed by age. Here you can ask “customers with outstanding balances”, “list overdue sales invoices” or “outstanding amount of <customer>”."),
	(re.compile(r"\b(check|see|view|find|track)\b.*\b(outstanding|payable)\b.*\b(supplier|payable|bill)", re.I),
		"In ERPNext, open Accounts > Accounts Payable to see what you owe each supplier. Here you can ask “list unpaid purchase invoices” or "
		"“outstanding amount of <supplier>”."),
]


def _doctype_alt():
	names = {dt.lower(): dt for dt in GUIDE}
	names.update({dt.lower() + "s": dt for dt in GUIDE if not dt.endswith("y")})
	names.update({"stock entries": "Stock Entry", "payment entries": "Payment Entry", "invoice": "Sales Invoice", "po": "Purchase Order",
		"so": "Sales Order", "grn": "Purchase Receipt"})
	return names


_NAMES = _doctype_alt()
_DT = "(?P<dt>" + "|".join(sorted(map(re.escape, _NAMES), key=len, reverse=True)) + ")"
_DT2 = _DT.replace("?P<dt>", "?P<dt2>")
_END = r"(?:\s+in\s+erpnext|\s+doctype|\s+document)?\s*[?.!]*\s*$"
WHAT_RE = re.compile(r"^\s*(?:what\s+is|what's|whats|what\s+are|explain|describe|define|tell\s+me\s+about)\s+(?:an?\s+|the\s+)?" + _DT + _END, re.I)
HOW_RE = re.compile(r"^\s*(?:explain\s+)?(?:how\s+(?:do|can|should|would)\s+(?:i|we|you)\s+|how\s+to\s+|(?:which|what)\s+steps\s+(?:are\s+)?(?:required|needed)\s+to\s+|"
	r"steps\s+to\s+)(?:create|make|raise|add|enter|record|prepare|do)\s+(?:an?\s+|new\s+|the\s+)?" + _DT + _END, re.I)
AFTER_RE = re.compile(r"^\s*what\s+happens\s+(?:after|when|once)\s+(?:i\s+|we\s+|you\s+)?submit(?:ting)?\s+(?:an?\s+|the\s+)?" + _DT + _END, re.I)
DIFF_RE = re.compile(r"\bdifference\s+between\s+(?:an?\s+|the\s+)?" + _DT + r"\s+and\s+(?:an?\s+|the\s+)?" + _DT2 + r"\b", re.I)
VS_RE = re.compile(r"^\s*" + _DT + r"\s+(?:vs\.?|versus)\s+" + _DT2 + r"\s*[?.!]*\s*$", re.I)
MODULE_RE = re.compile(r"^\s*(?:what\s+(?:does|is)|explain|describe|tell\s+me\s+about)\s+(?:the\s+)?(?P<module>" + "|".join(
	sorted(map(re.escape, MODULE_ALIASES), key=len, reverse=True)) + r")\s+module(?:\s+(?:do|for|in\s+erpnext))?\s*[?.!]*\s*$", re.I)


def abilities(doctype):
	"""What the chat can do with `doctype`: {"query": bool, "create": bool, "masters": bool}."""
	from ai_erpnext.chat.forms import CREATABLE, MASTERS
	from ai_erpnext.chat.queries import REGISTRY

	return {"query": doctype in REGISTRY, "create": doctype in CREATABLE, "master": doctype in MASTERS}


def footer(doctype):
	"""One accurate sentence on what the chat itself can do with this DocType."""
	can = abilities(doctype)
	plural = doctype.lower()[:-1] + "ies" if doctype.endswith("y") else doctype.lower() + "s"
	parts = []
	if can["query"]:
		parts.append(f"look up {plural} (e.g. “show the last 10 {plural}”)")
	if can["create"]:
		parts.append(f"create a {'new ' if can['master'] else 'Draft '}{doctype} step by step (say “create {doctype.lower()}”)")
	if not parts:
		return f"I can't look up or create {plural} in this chat yet."
	text = "In this chat I can " + " and ".join(parts) + "."
	if can["create"] and not can["master"]:
		text += " I never submit it; you review and submit it in ERPNext."
	elif not can["create"]:
		text += f" I can't create {plural} from the chat."
	return text


def match(text):
	"""(kind, subject) for a general question this registry can answer, or None."""
	for kind, regex in (("how", HOW_RE), ("after", AFTER_RE), ("what", WHAT_RE)):
		m = regex.match(text or "")
		if m:
			return kind, _NAMES[m.group("dt").lower()]
	m = DIFF_RE.search(text or "") or VS_RE.match(text or "")
	if m:
		return "compare", (_NAMES[m.group("dt").lower()], _NAMES[m.group("dt2").lower()])
	m = MODULE_RE.match(text or "")
	if m:
		return "module", MODULE_ALIASES[m.group("module").lower()]
	for regex, answer_text in TOPICS:
		if regex.search(text or ""):
			return "topic", answer_text
	return None


def answer(text):
	"""Deterministic general guidance (used when no AI provider is available), or None."""
	found = match(text)
	if not found:
		return None
	kind, subject = found
	if kind == "topic":
		return subject
	if kind == "module":
		doctypes = MODULES[subject]
		return f"{MODULE_WHAT[subject]} Main documents: {', '.join(doctypes)}. " + _module_abilities(doctypes)
	if kind == "compare":
		a, b = subject
		text = COMPARE.get(frozenset((a, b))) or f"{GUIDE[a]['what']} {GUIDE[b]['what']}"
		return text
	guide = GUIDE[subject]
	if kind == "how":
		steps = "\n".join(f"{i}. {step}" for i, step in enumerate(guide["create"], start=1))
		return f"To create a {subject} in ERPNext:\n{steps}\n{footer(subject)}"
	if kind == "after":
		return (guide.get("after_submit") or guide["what"]) + " " + footer(subject)
	return f"{guide['what']} {footer(subject)}"


def _module_abilities(doctypes):
	can_query = [dt for dt in doctypes if abilities(dt)["query"]]
	can_create = [dt for dt in doctypes if abilities(dt)["create"]]
	text = ""
	if can_query:
		text += f"Here I can look up {', '.join(can_query)}"
	if can_create:
		text += ("; " if text else "Here I can ") + f"create {', '.join(can_create)} (transactions as Drafts)"
	return (text + ".") if text else "I can't work with these documents in the chat yet."


def capability_summary():
	"""For the router prompt: what the assistant itself can and cannot do."""
	from ai_erpnext.chat.forms import CREATABLE
	from ai_erpnext.chat.queries import REGISTRY

	return (f"The assistant can look up (read-only): {', '.join(REGISTRY)}. It can create, after the user confirms a preview: "
		f"{', '.join(CREATABLE)} (transactions only as Drafts). It cannot submit, cancel, amend, delete, pay or create anything else "
		"(no Stock Entries, Payment Entries, Journal Entries); say so instead of offering it.")
