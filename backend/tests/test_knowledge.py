"""Knowledge base tests (M-08): extraction, overwrite, soft delete, injection."""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select

from app.core.exceptions import (
    KnowledgeEmptyError,
    KnowledgeError,
    KnowledgeUnsupportedError,
)
from app.models.knowledge_doc import KnowledgeDoc
from app.services.knowledge import MAX_KB_BYTES, KnowledgeService, extract_text


def make_pdf(text: str) -> bytes:
    """Build a tiny valid single-page PDF with correct xref offsets."""

    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode()
    )
    return bytes(out)


def make_docx(text: str) -> bytes:
    from docx import Document

    buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph(text)
    doc.save(buf)
    return buf.getvalue()


def test_extract_md() -> None:
    assert extract_text("faq.md", b"# FAQ\nReturn window: 30 days.").strip() == (
        "# FAQ\nReturn window: 30 days."
    )


def test_extract_pdf() -> None:
    text = extract_text("policy.pdf", make_pdf("Refund policy: 30 days no restocking fee"))
    assert "30 days" in text


def test_extract_docx() -> None:
    text = extract_text("warranty.docx", make_docx("Warranty: 1 year, returns accepted"))
    assert "1 year" in text


def test_unsupported_type_raises() -> None:
    with pytest.raises(KnowledgeUnsupportedError):
        extract_text("note.txt", b"hello")


def test_upload_then_overwrite_bumps_version(db) -> None:
    service = KnowledgeService(db)
    first = service.upload("faq.md", b"# FAQ v1\nOld content.")
    assert first.version == 1
    second = service.upload("faq.md", b"# FAQ v2\nNew content.")
    assert second.id == first.id
    assert second.version == 2
    assert "New content" in second.content
    docs = db.execute(select(KnowledgeDoc)).scalars().all()
    assert len(docs) == 1  # overwritten, not duplicated


def test_soft_delete_hides_doc_and_full_text(db) -> None:
    service = KnowledgeService(db)
    doc = service.upload("a.md", b"Alpha content.")
    service.upload("b.md", b"Beta content.")
    assert "Alpha content" in service.full_text()
    assert "Beta content" in service.full_text()
    assert service.soft_delete(doc.id) is True
    assert service.soft_delete(doc.id) is False  # already deleted
    assert "Alpha content" not in service.full_text()
    assert "Beta content" in service.full_text()
    assert service.get(doc.id) is None


def test_empty_extraction_rejected(db) -> None:
    with pytest.raises(KnowledgeEmptyError):
        KnowledgeService(db).upload("blank.md", b"   \n  ")


def test_size_limit_rejected(db) -> None:
    with pytest.raises(KnowledgeError):
        KnowledgeService(db).upload("big.md", b"x" * (MAX_KB_BYTES + 1))


def test_upload_sanitizes_filename(db) -> None:
    doc = KnowledgeService(db).upload("../../etc/faq.md", b"# FAQ\nSafe.")
    assert doc.filename == "faq.md"
