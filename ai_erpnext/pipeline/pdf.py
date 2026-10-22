"""Single-page PDF documents, read as an image of their page.

The PDF is checked and its only page is rendered to a PNG; that PNG then goes through the unchanged image pipeline (the
same image checks, extraction prompt, providers, mapping and confirmation). Nothing else about extraction changes, so the
existing image evaluation still applies.

Refused, with a reason the user can act on, never guessed around:
- more than one page (a page that isn't read could hold lines, totals or the other party of the document);
- encrypted, malformed or not really a PDF (the bytes must start with %PDF-, whatever the file name says);
- oversized files or pages (bounded bytes, page size and rendered pixels).

Rendering uses PDFium through pypdfium2, which ERPNext already installs (as pdfplumber's render backend). PDFium only
draws the page: no JavaScript, form actions or embedded files are run or extracted.
"""

import io

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PAGE_POINTS = 3000  # ~106 cm: far above any invoice or delivery note page; stops absurd page sizes
LONG_SIDE_PX = 2000  # the rendered page's longer side: legible for extraction, well under the image limits
NOT_A_PDF = "The attached file is not a readable PDF. Please attach a PDF, JPG, PNG or WEBP of the document."
MULTI_PAGE = (
	"That PDF has {pages} pages. I can only read single-page PDFs, so that no page is left out: please attach each page as "
	"an image, or a one-page PDF."
)


def problem(content):
	"""None if `content` is a readable, single-page, unencrypted PDF of sane size, else a user-safe reason."""
	import pypdfium2 as pdfium

	if not isinstance(content, (bytes, bytearray)) or not content.startswith(b"%PDF-"):
		return NOT_A_PDF
	if len(content) > MAX_PDF_BYTES:
		return "That PDF is larger than 10 MB. Please attach a smaller file."
	try:
		pdf = pdfium.PdfDocument(bytes(content))
	except Exception:
		return NOT_A_PDF  # malformed, or password-protected (PDFium can't open it without the password)
	try:
		pages = len(pdf)
		if pages != 1:
			return MULTI_PAGE.format(pages=pages) if pages else NOT_A_PDF
		width, height = pdf[0].get_size()
		if not (0 < width <= MAX_PAGE_POINTS and 0 < height <= MAX_PAGE_POINTS):
			return "That PDF's page has an unusual size I can't read. Please attach an image of the document."
	except Exception:
		return NOT_A_PDF
	finally:
		pdf.close()
	return None


def render(content):
	"""PNG bytes of the only page of a PDF that passed problem()."""
	import pypdfium2 as pdfium

	pdf = pdfium.PdfDocument(bytes(content))
	try:
		page = pdf[0]
		width, height = page.get_size()
		bitmap = page.render(scale=LONG_SIDE_PX / max(width, height))
		image = bitmap.to_pil()
		buffer = io.BytesIO()
		image.save(buffer, format="PNG")
		return buffer.getvalue()
	finally:
		pdf.close()
