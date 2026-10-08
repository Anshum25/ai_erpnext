"""Arithmetic checks recomputed in Python from extracted values."""

from frappe.utils import flt

from ai_erpnext.validation.base import DATA, ValidationReport

EXACT_TOLERANCE = 0.005  # below half a cent counts as equal


def rounding_tolerance(expected):
	"""Differences above this are real errors; between 0.01 and this they are rounding warnings."""
	return max(0.05, abs(flt(expected)) * 0.001)


def compare(report: ValidationReport, code, label, expected, actual, field=None):
	diff = round(abs(flt(expected) - flt(actual)), 6)
	if diff <= EXACT_TOLERANCE:
		return True
	if diff <= rounding_tolerance(expected):
		report.warning(code, f"{label}: expected {flt(expected, 2)}, document shows {flt(actual, 2)} (rounding difference)", field)
		return True
	report.error(code, f"{label}: expected {flt(expected, 2)}, document shows {flt(actual, 2)}", field, category=DATA)
	return False


def validate_line(report: ValidationReport, idx, qty, rate, amount, field):
	if qty is None:
		report.error("qty_missing", f"Row {idx}: quantity missing", field, category=DATA)
	elif qty <= 0:
		report.error("qty_not_positive", f"Row {idx}: quantity must be greater than 0 (got {qty})", field, category=DATA)

	if rate is None:
		report.error("rate_missing", f"Row {idx}: rate missing", field, category=DATA)
	elif rate < 0:
		report.error("rate_negative", f"Row {idx}: rate cannot be negative (got {rate})", field, category=DATA)

	if amount is not None and qty is not None and rate is not None:
		compare(report, "amount_mismatch", f"Row {idx} amount (qty x rate)", qty * rate, amount, field)


def validate_totals(report: ValidationReport, canonical: dict):
	"""Validate subtotal / tax / grand total where the document provides them."""
	lines = [r for r in canonical.get("items", []) if r.get("amount") is not None or (r.get("qty") is not None and r.get("rate") is not None)]
	line_total = sum(flt(r["amount"]) if r.get("amount") is not None else flt(r["qty"]) * flt(r["rate"]) for r in lines)

	subtotal = canonical.get("subtotal")
	if subtotal is not None and lines:
		compare(report, "subtotal_mismatch", "Subtotal (sum of lines)", line_total, subtotal, "subtotal")
	base = subtotal if subtotal is not None else line_total

	tax_rate, tax_amount = canonical.get("tax_rate"), canonical.get("tax_amount")
	if tax_rate is not None and tax_rate > 1:
		tax_rate = tax_rate / 100.0
	if tax_rate is not None and tax_amount is not None and base:
		compare(report, "tax_mismatch", f"Tax ({flt(tax_rate * 100, 2)}% of {flt(base, 2)})", base * tax_rate, tax_amount, "tax_amount")
	elif tax_rate is not None and tax_amount is None and base:
		# Extraction sometimes returns only the rate; derive the amount for the total check.
		tax_amount = flt(base) * tax_rate

	total = canonical.get("total_amount")
	if total is not None and (lines or subtotal is not None):
		expected = flt(base) - flt(canonical.get("discount_amount")) + flt(tax_amount)
		compare(report, "total_mismatch", "Grand total (subtotal - discount + tax)", expected, total, "total_amount")
