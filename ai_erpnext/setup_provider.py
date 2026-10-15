"""Switch AI Provider Settings safely and keep a restorable copy of the previous provider.

Secrets are never printed. The previous API key is kept encrypted in Frappe's __Auth
store (same mechanism as Password fields); the non-secret settings are kept as a global
default. Usage (key read from a private file, never from the command line):

    bench --site <site> execute ai_erpnext.setup_provider.configure_groq --kwargs '{"key_file": "/path/to/0600-file"}'
    bench --site <site> execute ai_erpnext.setup_provider.show_provider
    bench --site <site> execute ai_erpnext.setup_provider.restore_previous_provider
"""

import json
import os

import frappe
from frappe.utils import cint
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from ai_erpnext.ai.providers import PROVIDERS, configuration_issues

SETTINGS = "AI Provider Settings"
BACKUP_KEY = "ai_erpnext_previous_provider"
BACKUP_PASSWORD_FIELD = "previous_api_key"
GROQ = {"provider": "Groq", "base_url": PROVIDERS["Groq"]["base_url"], "model": PROVIDERS["Groq"]["model"]}


def show_provider():
	"""Non-secret summary of the configuration (keys are reported only as set / not set)."""
	s = frappe.get_single(SETTINGS)
	backup = json.loads(frappe.db.get_global(BACKUP_KEY) or "null")
	errors, warnings = configuration_issues(s)
	print(json.dumps({"provider": s.provider, "base_url": s.base_url, "model": s.model, "api_key_set": bool(s.api_key),
		"fallback_enabled": cint(s.get("fallback_enabled")), "fallback_provider": s.get("fallback_provider"),
		"fallback_model": s.get("fallback_model"), "fallback_api_key_set": bool(s.get("fallback_api_key")),
		"fallback_daily_limit": cint(s.get("fallback_daily_limit")), "errors": errors, "warnings": warnings,
		"previous_provider_backup": backup, "previous_api_key_stored": bool(_previous_key())}))


def backup_current_provider():
	s = frappe.get_single(SETTINGS)
	frappe.db.set_global(BACKUP_KEY, json.dumps({"provider": s.provider, "base_url": s.base_url, "model": s.model}))
	if s.api_key:
		set_encrypted_password(SETTINGS, SETTINGS, s.get_password("api_key"), BACKUP_PASSWORD_FIELD)


def configure_groq(key_file):
	key = _read_key(key_file)
	s = frappe.get_single(SETTINGS)
	if s.provider != GROQ["provider"]:
		backup_current_provider()  # keep the previous provider restorable
	s.update(GROQ)
	s.api_key = key
	s.save()
	frappe.db.commit()
	show_provider()


def restore_previous_provider():
	backup = json.loads(frappe.db.get_global(BACKUP_KEY) or "null")
	key = _previous_key()
	if not backup or not key:
		frappe.throw("No previous provider backup found")
	s = frappe.get_single(SETTINGS)
	s.update(backup)
	s.api_key = key
	s.save()
	frappe.db.commit()
	show_provider()


def _previous_key():
	return get_decrypted_password(SETTINGS, SETTINGS, BACKUP_PASSWORD_FIELD, raise_exception=False)


def _read_key(path):
	if not os.path.isfile(path):
		frappe.throw("Key file not found")
	if os.stat(path).st_mode & 0o077:
		frappe.throw("Key file must be private (chmod 600)")
	line = open(path).read().strip()
	key = line.split("=", 1)[1].strip() if "=" in line else line
	if not key:
		frappe.throw("Key file is empty")
	return key
