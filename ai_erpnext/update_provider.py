"""Retired: this one-off script used to contain an API key in source.

API keys are never stored in code. Configure providers in AI Provider Settings (Desk), or
with a private key file:

    bench --site <site> execute ai_erpnext.setup_provider.configure_groq --kwargs '{"key_file": "/path/to/0600-file"}'
"""

import frappe


def execute():
	frappe.throw(
		frappe._(
			"Retired. Set the provider and key in AI Provider Settings, or use ai_erpnext.setup_provider."
		)
	)
