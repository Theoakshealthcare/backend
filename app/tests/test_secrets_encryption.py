"""Encryption key rotation: MultiFernet keys + scripts/reencrypt_health_data.py."""

import pytest
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import text

import app.core.security as security
from app.db.models.fallguard import FallGuardAssessment
from app.db.models.patient_vital import PatientVital
from app.tests.factories import make_patient
from scripts import reencrypt_health_data

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()


def use_keys(monkeypatch, *keys):
    monkeypatch.setattr(security, "fernet", security._build_fernet(",".join(keys)))


def _raw(db, table, column, row_id):
    return db.execute(text(f'SELECT "{column}" FROM {table} WHERE id = :id'), {"id": row_id}).scalar_one()


def _seed(db, patient):
    vital = PatientVital(patient_id=patient.id, recorded_by_user_id=patient.user_id, vital_type="heart_rate")
    vital.value = "72"
    vital.notes = "resting"
    assessment = FallGuardAssessment(patient_id=patient.id, total_score=5)
    assessment.ai_summary = "low risk summary"
    db.add_all([vital, assessment])
    db.commit()
    return vital.id, assessment.id


def test_build_fernet_rejects_empty_key_list():
    with pytest.raises(RuntimeError):
        security._build_fernet(" , ")


def test_first_key_encrypts_and_any_key_decrypts(monkeypatch):
    use_keys(monkeypatch, OLD_KEY)
    token = security.encrypt("secret")
    use_keys(monkeypatch, NEW_KEY, OLD_KEY)
    assert security.decrypt(token) == "secret"
    rotated = security.rotate_ciphertext(token)
    assert MultiFernet([Fernet(NEW_KEY)]).decrypt(rotated.encode()) == b"secret"


def test_reencrypt_moves_all_columns_to_new_key(db, monkeypatch):
    patient = make_patient(db)
    use_keys(monkeypatch, OLD_KEY)
    vital_id, assessment_id = _seed(db, patient)
    # a legacy plaintext vital, as the model tolerates on read
    db.execute(text("UPDATE patient_vitals SET notes = 'legacy plaintext' WHERE id = :id"), {"id": vital_id})
    db.commit()

    use_keys(monkeypatch, NEW_KEY, OLD_KEY)
    results = reencrypt_health_data.reencrypt()
    assert results["patient_vitals.value"].rotated == 1
    assert results["patient_vitals.notes"].encrypted_plaintext == 1
    assert results["fallguard_assessments.ai_summary_encrypted"].rotated == 1

    # with only the new key, everything is still readable
    use_keys(monkeypatch, NEW_KEY)
    db.expire_all()
    assert security.decrypt(_raw(db, "patient_vitals", "value", vital_id)) == "72"
    assert security.decrypt(_raw(db, "patient_vitals", "notes", vital_id)) == "legacy plaintext"
    assert security.decrypt(_raw(db, "fallguard_assessments", "ai_summary_encrypted", assessment_id)) == "low risk summary"


def test_dry_run_writes_nothing(db, monkeypatch):
    patient = make_patient(db)
    use_keys(monkeypatch, OLD_KEY)
    vital_id, _ = _seed(db, patient)
    before = _raw(db, "patient_vitals", "value", vital_id)

    use_keys(monkeypatch, NEW_KEY, OLD_KEY)
    reencrypt_health_data.reencrypt(dry_run=True)
    db.expire_all()
    assert _raw(db, "patient_vitals", "value", vital_id) == before
    use_keys(monkeypatch, NEW_KEY)
    with pytest.raises(InvalidToken):
        security.decrypt(before)


def test_unreadable_values_fail_the_run(db, monkeypatch):
    patient = make_patient(db)
    use_keys(monkeypatch, OLD_KEY)
    vital_id, _ = _seed(db, patient)
    before = _raw(db, "patient_vitals", "value", vital_id)
    use_keys(monkeypatch, NEW_KEY)  # old key missing: nothing can be read
    assert reencrypt_health_data.main([]) == 1
    # ciphertext is never mistaken for legacy plaintext and double-encrypted
    db.expire_all()
    assert _raw(db, "patient_vitals", "value", vital_id) == before


@pytest.mark.parametrize("missing", ["SECRET_KEY", "DATABASE_URL", "VITALS_ENCRYPTION_KEY"])
def test_app_refuses_to_start_without_required_settings(missing):
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != missing}
    env["OAKLY_ENV_FILE"] = ""
    result = subprocess.run(
        [sys.executable, "-c", "import app.core.config"], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert missing in result.stderr
