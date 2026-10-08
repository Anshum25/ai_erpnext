from ai_erpnext.mapping.base import BaseMatcher


class CustomerMatcher(BaseMatcher):
	doctype = "Customer"
	title_field = "customer_name"
	base_filters = {"disabled": 0}
	extra_fields = ("default_currency",)


def match_customer(value):
	return CustomerMatcher().match(value)
