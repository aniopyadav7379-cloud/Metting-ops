"""Tests for api/documents.py + services/document_knowledge.py.

Real extraction (python-docx builds a genuine .docx in-memory; TXT/MD are
trivial; PDF extraction is unit-tested against pypdf's own text-writing
round trip) and real (embedded) Qdrant indexing — no mocking of the
extraction libraries or the vector store itself.
"""
from __future__ import annotations

import hashlib
import io

import pytest
from qdrant_client import QdrantClient


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _fake_dense_vector(text: str, dim: int = 32) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


@pytest.fixture()
def real_qdrant(monkeypatch):
    from services.semantic_search_service import semantic_search

    memory_client = QdrantClient(":memory:")
    monkeypatch.setattr(semantic_search, "_client", None)
    monkeypatch.setattr(semantic_search, "_dense_dim", 32)
    monkeypatch.setattr(semantic_search, "_get_client", lambda: memory_client)
    monkeypatch.setattr(semantic_search, "_embed", lambda texts: [_fake_dense_vector(t, 32) for t in texts])
    monkeypatch.setattr(semantic_search, "_sparse_embed", lambda texts: None)
    monkeypatch.setattr(semantic_search, "_hybrid_enabled", False)
    yield semantic_search


def _make_docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document

    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_pdf_bytes(text_lines: list[str]) -> bytes:
    """A minimal real single-page PDF with actual extractable text, built
    directly from the PDF content-stream spec rather than via a
    third-party PDF-writer that doesn't support text (pypdf itself can't
    author text streams). This is a genuine PDF pypdf will parse and
    extract text from — not a mock."""
    content_lines = ["BT", "/F1 12 Tf", "72 720 Td", "14 TL"]
    for line in text_lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content_lines.append(f"({escaped}) Tj T*")
    content_lines.append("ET")
    content_stream = "\n".join(content_lines).encode("latin-1")

    objects = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> /MediaBox [0 0 612 792] /Contents 5 0 R >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(f"<< /Length {len(content_stream)} >>\nstream\n".encode("latin-1") + content_stream + b"\nendstream")

    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(buf.tell())
        buf.write(f"{i} 0 obj\n".encode("latin-1"))
        buf.write(obj)
        buf.write(b"\nendobj\n")
    xref_offset = buf.tell()
    buf.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode("latin-1"))
    buf.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("latin-1"))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Extraction — real, unmocked
# ---------------------------------------------------------------------------

def test_extract_text_from_txt():
    from services.document_knowledge import extract_text

    assert extract_text("notes.txt", b"Hello world, this is a plain text note.") == "Hello world, this is a plain text note."


def test_extract_text_from_md():
    from services.document_knowledge import extract_text

    text = extract_text("readme.md", "# Title\n\nSome **markdown** content.".encode("utf-8"))
    assert "Title" in text and "markdown" in text


def test_extract_text_from_real_docx():
    from services.document_knowledge import extract_text

    data = _make_docx_bytes(["Architecture Decision Record", "We chose Postgres over MySQL for JSONB support."])
    text = extract_text("adr.docx", data)
    assert "Architecture Decision Record" in text
    assert "Postgres over MySQL" in text


def test_extract_text_from_real_pdf():
    from services.document_knowledge import extract_text

    data = _make_pdf_bytes(["Security Policy", "All API keys must be rotated every 90 days."])
    text = extract_text("policy.pdf", data)
    assert "Security Policy" in text
    assert "rotated every 90 days" in text


def test_extract_rejects_unsupported_extension():
    from services.document_knowledge import extract_text, UnsupportedDocumentType

    with pytest.raises(UnsupportedDocumentType):
        extract_text("archive.zip", b"PK\x03\x04")


def test_extract_rejects_oversized_document():
    from services.document_knowledge import extract_text, DocumentTooLarge, MAX_DOCUMENT_BYTES

    with pytest.raises(DocumentTooLarge):
        extract_text("huge.txt", b"a" * (MAX_DOCUMENT_BYTES + 1))


# ---------------------------------------------------------------------------
# API: upload / list / get / dedup / delete / reindex
# ---------------------------------------------------------------------------

def test_upload_txt_document_indexes_into_real_qdrant(client, real_qdrant):
    headers = _admin_bearer(client)
    resp = client.post(
        "/api/documents/upload",
        headers=headers,
        files={"file": ("policy.txt", b"All engineers must complete security training annually.", "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "indexed"
    assert body["filename"] == "policy.txt"

    results = real_qdrant.search_chunks(query="security training", limit=5, organization_id=1)
    assert any(body["doc_id"] == r["session_id"] for r in results)


def test_upload_docx_document(client, real_qdrant):
    headers = _admin_bearer(client)
    data = _make_docx_bytes(["Deployment Runbook", "Run migrations before restarting the API service."])
    resp = client.post(
        "/api/documents/upload",
        headers=headers,
        files={"file": ("runbook.docx", data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "indexed"


def test_duplicate_upload_does_not_create_second_document(client, real_qdrant):
    headers = _admin_bearer(client)
    payload = b"Identical content uploaded twice should not duplicate."
    r1 = client.post("/api/documents/upload", headers=headers, files={"file": ("a.txt", payload, "text/plain")})
    r2 = client.post("/api/documents/upload", headers=headers, files={"file": ("b.txt", payload, "text/plain")})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["doc_id"] == r2.json()["doc_id"]

    listing = client.get("/api/documents", headers=headers)
    matching = [d for d in listing.json() if d["doc_id"] == r1.json()["doc_id"]]
    assert len(matching) == 1


def test_unsupported_file_type_rejected(client):
    headers = _admin_bearer(client)
    resp = client.post(
        "/api/documents/upload",
        headers=headers,
        files={"file": ("archive.zip", b"PK\x03\x04", "application/zip")},
    )
    assert resp.status_code == 422


def test_empty_file_rejected(client):
    headers = _admin_bearer(client)
    resp = client.post("/api/documents/upload", headers=headers, files={"file": ("empty.txt", b"", "text/plain")})
    assert resp.status_code == 422


def test_delete_document_removes_qdrant_points(client, real_qdrant):
    headers = _admin_bearer(client)
    resp = client.post(
        "/api/documents/upload",
        headers=headers,
        files={"file": ("temp.txt", b"This document will be deleted from the knowledge base.", "text/plain")},
    )
    doc_id = resp.json()["doc_id"]
    assert real_qdrant.search_chunks(query="deleted from the knowledge base", limit=5, organization_id=1)

    delete_resp = client.delete(f"/api/documents/{doc_id}", headers=headers)
    assert delete_resp.status_code == 200

    remaining = real_qdrant.search_chunks(query="deleted from the knowledge base", limit=5, organization_id=1)
    assert all(r["session_id"] != doc_id for r in remaining)

    get_resp = client.get(f"/api/documents/{doc_id}", headers=headers)
    assert get_resp.status_code == 404


def test_documents_endpoint_requires_auth(client):
    resp = client.get("/api/documents")
    assert resp.status_code in (401, 403)


def test_documents_are_tenant_isolated(client, real_qdrant):
    """A document uploaded under org 1 must not surface in a Qdrant search
    scoped to a different organization."""
    headers = _admin_bearer(client)
    client.post(
        "/api/documents/upload",
        headers=headers,
        files={"file": ("confidential.txt", b"Merger discussions with Acme Corp are strictly confidential.", "text/plain")},
    )
    org2_results = real_qdrant.search_chunks(query="Merger discussions with Acme Corp", limit=5, organization_id=2)
    assert org2_results == []
