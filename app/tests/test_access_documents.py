"""WP-C item 7: caregiver documents are private."""

import os
import uuid
from pathlib import Path

import pytest

from app.core.config import settings
from app.db.models.caregiver import CaregiverDocument
from app.tests.factories import (
    auth_headers,
    make_admin,
    make_caregiver,
    make_patient,
    refresh_headers,
)
from scripts.migrate_private_documents import migrate

UPLOAD = "/api/v1/caregiver/documents/upload"
PDF = b"%PDF-1.4 fake pdf for tests"


@pytest.fixture
def private_dir(tmp_path, monkeypatch):
    path = tmp_path / "private"
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(path))
    return path


def upload(client, caregiver, filename="licence.pdf", content=PDF, content_type="application/pdf"):
    return client.post(
        UPLOAD,
        files={"file": (filename, content, content_type)},
        data={"document_type": "license"},
        headers=auth_headers(caregiver.user),
    )


def make_document(db, caregiver, file_path, mime="application/pdf"):
    doc = CaregiverDocument(
        caregiver_id=caregiver.id,
        document_type="license",
        document_name="licence.pdf",
        file_path=file_path,
        file_size=10,
        mime_type=mime,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


# --- storage location -----------------------------------------------------------


def test_upload_is_stored_in_the_private_dir_not_the_public_mount(db, client, private_dir):
    caregiver = make_caregiver(db)
    r = upload(client, caregiver)
    assert r.status_code == 200, r.text

    stored = [p for p in private_dir.rglob("*") if p.is_file()]
    assert len(stored) == 1 and stored[0].read_bytes() == PDF
    assert not os.path.exists(f"uploads/caregivers/{caregiver.id}")

    # the DB stores a key relative to PRIVATE_UPLOAD_DIR, never a path under /uploads
    doc = db.query(CaregiverDocument).one()
    assert not doc.file_path.startswith("uploads")
    assert (private_dir / doc.file_path).is_file()


def test_hostile_filename_cannot_escape_the_private_dir(db, client, private_dir):
    caregiver = make_caregiver(db)
    r = upload(client, caregiver, filename="x.pdf/../../../evil")
    assert r.status_code == 200, r.text
    for p in private_dir.rglob("*"):
        assert private_dir in p.resolve().parents or p.resolve() == private_dir.resolve()
    doc = db.query(CaregiverDocument).one()
    assert ".." not in doc.file_path
    assert (private_dir / doc.file_path).resolve().is_relative_to(private_dir.resolve())


def test_uploads_static_mount_serves_only_profile_images(client):
    marker = uuid.uuid4().hex
    secret = Path("uploads/caregivers/legacy-test") / f"{marker}.pdf"
    public = Path("uploads/profile_images") / f"{marker}.png"
    secret.parent.mkdir(parents=True, exist_ok=True)
    public.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(PDF)
    public.write_bytes(b"png")
    try:
        assert client.get(f"/uploads/profile_images/{marker}.png").status_code == 200
        assert client.get(f"/uploads/caregivers/legacy-test/{marker}.pdf").status_code == 404
    finally:
        secret.unlink(missing_ok=True)
        public.unlink(missing_ok=True)
        secret.parent.rmdir()


# --- GET /caregiver/documents/{id}/file ------------------------------------------


def uploaded_document(db, client, caregiver):
    assert upload(client, caregiver).status_code == 200
    return db.query(CaregiverDocument).filter(CaregiverDocument.caregiver_id == caregiver.id).one()


def test_owner_can_download_own_document(db, client, private_dir):
    caregiver = make_caregiver(db)
    doc = uploaded_document(db, client, caregiver)
    r = client.get(f"/api/v1/caregiver/documents/{doc.id}/file", headers=auth_headers(caregiver.user))
    assert r.status_code == 200
    assert r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("inline")


def test_admin_can_download_any_document(db, client, private_dir):
    caregiver = make_caregiver(db)
    doc = uploaded_document(db, client, caregiver)
    admin = make_admin(db)
    r = client.get(f"/api/v1/caregiver/documents/{doc.id}/file", headers=auth_headers(admin))
    assert r.status_code == 200 and r.content == PDF


def test_download_requires_a_token(db, client, private_dir):
    caregiver = make_caregiver(db)
    doc = uploaded_document(db, client, caregiver)
    url = f"/api/v1/caregiver/documents/{doc.id}/file"
    assert client.get(url).status_code == 401
    assert client.get(url, headers=refresh_headers(caregiver.user)).status_code == 401


def test_other_caregiver_and_patient_cannot_download(db, client, private_dir):
    owner, other = make_caregiver(db), make_caregiver(db)
    patient = make_patient(db)
    doc = uploaded_document(db, client, owner)
    url = f"/api/v1/caregiver/documents/{doc.id}/file"
    assert client.get(url, headers=auth_headers(other.user)).status_code == 403
    assert client.get(url, headers=auth_headers(patient.user)).status_code == 403


def test_unknown_document_is_404(db, client, private_dir):
    caregiver = make_caregiver(db)
    r = client.get(f"/api/v1/caregiver/documents/{uuid.uuid4()}/file", headers=auth_headers(caregiver.user))
    assert r.status_code == 404


def test_row_pointing_outside_the_private_dir_is_never_served(db, client, private_dir, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("top secret")
    private_dir.mkdir()
    caregiver = make_caregiver(db)
    for bad in (str(outside), "../secret.txt", "../../etc/passwd"):
        doc = make_document(db, caregiver, bad)
        r = client.get(f"/api/v1/caregiver/documents/{doc.id}/file", headers=auth_headers(caregiver.user))
        assert r.status_code == 404, bad
        assert b"top secret" not in r.content


def test_missing_file_on_disk_is_404(db, client, private_dir):
    caregiver = make_caregiver(db)
    doc = make_document(db, caregiver, "caregivers/x/documents/gone.pdf")
    r = client.get(f"/api/v1/caregiver/documents/{doc.id}/file", headers=auth_headers(caregiver.user))
    assert r.status_code == 404


# --- one-off migration script -----------------------------------------------------


def test_migration_moves_legacy_files_and_rewrites_paths(db, tmp_path):
    legacy_root = tmp_path / "uploads"
    private_root = tmp_path / "private"
    caregiver = make_caregiver(db)
    legacy_file = legacy_root / "caregivers" / str(caregiver.id) / "documents" / "a.pdf"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_bytes(PDF)
    legacy_doc = make_document(db, caregiver, f"uploads/caregivers/{caregiver.id}/documents/a.pdf")
    missing_doc = make_document(db, caregiver, f"uploads/caregivers/{caregiver.id}/documents/missing.pdf")
    done_doc = make_document(db, caregiver, f"caregivers/{caregiver.id}/documents/already.pdf")

    stats = migrate(db, legacy_root=legacy_root, private_root=private_root)

    db.expire_all()
    key = f"caregivers/{caregiver.id}/documents/a.pdf"
    assert db.get(CaregiverDocument, legacy_doc.id).file_path == key
    assert (private_root / key).read_bytes() == PDF
    assert not legacy_file.exists()
    # not-found files are reported and left alone; already-migrated rows are skipped
    assert db.get(CaregiverDocument, missing_doc.id).file_path.startswith("uploads/")
    assert db.get(CaregiverDocument, done_doc.id).file_path == f"caregivers/{caregiver.id}/documents/already.pdf"
    assert (stats.moved, stats.missing, stats.skipped) == (1, 1, 1)


def test_migration_is_idempotent_and_supports_dry_run(db, tmp_path):
    legacy_root, private_root = tmp_path / "uploads", tmp_path / "private"
    caregiver = make_caregiver(db)
    legacy_file = legacy_root / "caregivers" / str(caregiver.id) / "documents" / "a.pdf"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_bytes(PDF)
    doc = make_document(db, caregiver, f"uploads/caregivers/{caregiver.id}/documents/a.pdf")

    dry = migrate(db, legacy_root=legacy_root, private_root=private_root, dry_run=True)
    db.expire_all()
    assert dry.moved == 1 and legacy_file.exists() and doc.file_path.startswith("uploads/")

    migrate(db, legacy_root=legacy_root, private_root=private_root)
    again = migrate(db, legacy_root=legacy_root, private_root=private_root)
    assert (again.moved, again.missing) == (0, 0)


def test_migration_refuses_paths_that_escape_the_legacy_root(db, tmp_path):
    legacy_root, private_root = tmp_path / "uploads", tmp_path / "private"
    legacy_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    caregiver = make_caregiver(db)
    doc = make_document(db, caregiver, "uploads/../outside.txt")

    stats = migrate(db, legacy_root=legacy_root, private_root=private_root)

    db.expire_all()
    assert outside.exists() and not private_root.exists()
    assert doc.file_path == "uploads/../outside.txt"
    assert stats.moved == 0 and stats.missing == 1
