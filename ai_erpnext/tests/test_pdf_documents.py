"""Single-page PDFs (pipeline/pdf.py): accepted and read as a PNG of the page; multi-page, encrypted, fake and absurdly
sized PDFs are refused before anything is stored or queued. The extraction itself is never run here (frappe.enqueue is
mocked): the image pipeline after the PNG is unchanged and tested elsewhere.

Run: bench --site <site> run-tests --module ai_erpnext.tests.test_pdf_documents
"""

import io
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from PIL import Image

from ai_erpnext.api import chat
from ai_erpnext.pipeline import pdf

ROUTE = "ai_erpnext.chat.llm.route"


def pil_pdf(pages=1, size=(600, 800)):
	images = [Image.new("RGB", size, "white") for _ in range(pages)]
	for n, img in enumerate(images):
		img.putpixel((n, n), (n, 0, 0))  # different content per call and page
	buf = io.BytesIO()
	images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
	return buf.getvalue()


def pypdf_pdf(width=595, height=842, password=None):
	from pypdf import PdfWriter

	writer = PdfWriter()
	writer.add_blank_page(width=width, height=height)
	if password:
		writer.encrypt(password)
	buf = io.BytesIO()
	writer.write(buf)
	return buf.getvalue()


class TestPdfDocuments(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("aipdf_test")
		self.addCleanup(frappe.db.rollback, save_point="aipdf_test")
		mock.patch(ROUTE, return_value={"mode": "chat", "reply": "ok"}).start()
		self.enqueue = mock.patch("frappe.enqueue").start()
		self.addCleanup(mock.patch.stopall)
		self.s = chat.new_conversation()["session"]

	def upload(self, content, name="aipdf-scan.pdf"):
		return (
			frappe.get_doc({"doctype": "File", "file_name": name, "content": content, "is_private": 1})
			.insert()
			.file_url
		)

	def send(self, file_url):
		r = chat.send_message(session=self.s, file_url=file_url)
		return " ".join(m.get("text") or "" for m in r["messages"])

	# --- the checks ----------------------------------------------------------------------------------
	def test_problems_are_found_before_rendering(self):
		self.assertIsNone(pdf.problem(pil_pdf()))
		self.assertIn("2 pages", pdf.problem(pil_pdf(pages=2)))
		self.assertEqual(pdf.problem(b"hello, I am not a pdf"), pdf.NOT_A_PDF)
		self.assertEqual(pdf.problem(b"%PDF-1.7 truncated garbage"), pdf.NOT_A_PDF)
		self.assertEqual(pdf.problem(pypdf_pdf(password="secret")), pdf.NOT_A_PDF)
		self.assertIn("unusual size", pdf.problem(pypdf_pdf(width=5000, height=5000)))
		with mock.patch.object(pdf, "MAX_PDF_BYTES", 10):
			self.assertIn("larger than 10 MB", pdf.problem(pil_pdf()))

	def test_the_page_is_rendered_to_a_bounded_png(self):
		png = pdf.render(pil_pdf(size=(600, 800)))
		with Image.open(io.BytesIO(png)) as img:
			self.assertEqual(img.format, "PNG")
			self.assertEqual(max(img.size), pdf.LONG_SIDE_PX)

	# --- through the chat ------------------------------------------------------------------------------
	def test_a_one_page_pdf_is_read_as_a_private_png_of_the_user(self):
		text = self.send(self.upload(pil_pdf()))
		self.assertEqual(self.enqueue.call_count, 1)
		entry = frappe.get_doc(
			"AI Document Entry", frappe.db.get_value("AI Chat Session", self.s, "active_entry")
		)
		self.assertTrue(entry.file.endswith("-page-1.png"), entry.file)
		page = frappe.get_doc("File", {"file_url": entry.file})
		self.assertEqual((page.is_private, page.owner), (1, "Administrator"))
		self.assertNotIn("not a readable", text)

	def test_refused_pdfs_store_and_queue_nothing(self):
		before = frappe.db.count("File", {"file_name": ["like", "%-page-1.png"]})
		# (a file that only pretends to be a PDF is already refused by Frappe's upload; pdf.problem is checked above)
		for content, reason in (
			(pil_pdf(pages=3), "3 pages"),
			(pypdf_pdf(password="secret"), "not a readable PDF"),
		):
			self.assertIn(reason, self.send(self.upload(content)))
		self.enqueue.assert_not_called()
		self.assertEqual(frappe.db.count("File", {"file_name": ["like", "%-page-1.png"]}), before)

	def test_another_users_pdf_is_not_read(self):
		url = self.upload(pil_pdf())
		frappe.db.set_value("File", {"file_url": url}, "owner", "Guest")
		self.assertIn("couldn't access that attachment", self.send(url))
		self.enqueue.assert_not_called()
