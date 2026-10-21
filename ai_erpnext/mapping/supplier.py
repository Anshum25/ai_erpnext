from ai_erpnext.mapping.base import BaseMatcher


class SupplierMatcher(BaseMatcher):
	doctype = "Supplier"
	title_field = "supplier_name"
	base_filters = {"disabled": 0}
	extra_fields = ("default_currency",)


def match_supplier(value):
	return SupplierMatcher().match(value)
