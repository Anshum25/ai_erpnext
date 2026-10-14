"""Shared test fixtures that do not depend on data already present on the site."""

import io

import frappe


def test_file(ext="png", owner="Administrator"):
	"""A File record (with real content) owned by `owner`; returns its file_url.

	Content is generated, so the tests work on a fresh CI site. The extraction itself is
	always mocked: the bytes are never sent anywhere.
	"""
	content = _image(ext)
	previous = frappe.session.user
	frappe.set_user(owner)
	try:
		doc = frappe.get_doc({"doctype": "File", "file_name": f"ai-erpnext-test-invoice.{ext}", "content": content,
			"is_private": 1}).insert()
	finally:
		frappe.set_user(previous)
	return doc.file_url


def _image(ext):
	"""A small valid image (or one-page PDF), different on every call.

	Unique content matters: Frappe gives files with identical content the same URL, and a
	shared URL would let tests see each other's attachments.
	"""
	import random

	from PIL import Image

	img = Image.new("RGB", (64, 48), "white")
	for x in range(64):
		img.putpixel((x, random.randrange(48)), (random.randrange(256), random.randrange(256), random.randrange(256)))
	buf = io.BytesIO()
	img.save(buf, format={"jpg": "JPEG", "jpeg": "JPEG"}.get(ext, ext.upper()))
	return buf.getvalue()
