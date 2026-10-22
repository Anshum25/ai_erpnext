"""Desk boot information for the chat widget.

ai_chat_enabled tells the widget whether to show its launcher at all: the user must be allowed to create AI Chat Session
records (System Manager, or the opt-in role "AI Assistant User"). It is only a UI decision; every chat endpoint checks the
same permission again on the server.
"""

import frappe


def boot_session(bootinfo):
	bootinfo.ai_chat_enabled = False
	if frappe.session.user == "Guest" or not frappe.db.exists("DocType", "AI Chat Session"):
		return
	if frappe.has_permission("AI Chat Session", "create"):
		bootinfo.ai_chat_enabled = True
