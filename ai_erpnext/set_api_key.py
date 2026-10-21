"""Retired: this one-off script used to contain an API key in source.

API keys are never stored in code. Set the key in AI Provider Settings (Desk), or use
ai_erpnext.setup_provider with a private key file.
"""

import frappe


def set_api_key():
	frappe.throw(
		frappe._(
			"Retired. Set the provider and key in AI Provider Settings, or use ai_erpnext.setup_provider."
		)
	)
