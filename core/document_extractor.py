import base64
import io
import re


MAX_CHARS = 80_000


def extract_text(mime_type: str, base64_data: str, filename: str) -> str:
    try:
        raw = base64.b64decode(base64_data)
        mt = mime_type.lower()
        fn = filename.lower()

        if mt == "application/pdf" or fn.endswith(".pdf"):
            text = _extract_pdf(raw)
        elif mt in (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        ) or fn.endswith((".docx", ".doc")):
            text = _extract_docx(raw)
        elif mt in (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        ) or fn.endswith((".xlsx", ".xls")):
            text = _extract_xlsx(raw)
        elif mt.startswith("text/") or fn.endswith((".txt", ".md", ".csv", ".tsv")):
            text = raw.decode("utf-8", errors="replace")
        elif mt in ("application/rtf", "text/rtf") or fn.endswith(".rtf"):
            text = _extract_rtf(raw)
        else:
            text = raw.decode("utf-8", errors="replace")

        header = f"[Document: {filename}]\n"
        footer = "\n[End of document]"
        if len(text) > MAX_CHARS:
            body = text[:MAX_CHARS] + f"\n... [truncated — {len(text) - MAX_CHARS} chars omitted]"
        else:
            body = text
        return header + body + footer

    except Exception as e:
        return f"[Document: {filename}]\n[Extraction failed: {e}]\n[End of document]"


def _extract_pdf(raw: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


def _extract_docx(raw: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(raw))
    return "\n".join(p.text for p in doc.paragraphs if p.text)


def _extract_xlsx(raw: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    sheets = []
    for sheet in wb.worksheets:
        rows = []
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) if c is not None else "" for c in row]
            if any(cells):
                rows.append("\t".join(cells))
        if rows:
            sheets.append(f"Sheet: {sheet.title}\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(sheets)


def _extract_rtf(raw: bytes) -> str:
    text = raw.decode("latin-1", errors="replace")
    text = re.sub(r"\{[^{}]*\}|\\[a-z]+\d* ?|[{}]", "", text)
    return text.strip()
