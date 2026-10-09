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
from frappe.utils.password import get_decrypted_password, set_encrypted_password

SETTINGS = "AI Provider Settings"
BACKUP_KEY = "ai_erpnext_previous_provider"
BACKUP_PASSWORD_FIELD = "previous_api_key"
GROQ = {"provider": "Groq", "base_url": "https://api.groq.com/openai/v1", "model": "qwen/qwen3.8-27b"}


def show_provider():
	s = frappe.get_single(SETTINGS)
	backup = json.loads(frappe.db.get_global(BACKUP_KEY) or "null")
	print(json.dumps({"provider": s.provider, "base_url": s.base_url, "model": s.model, "api_key_set": bool(s.api_key),
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
