"""Re-encrypt every encrypted health column with the primary encryption key.

Use after rotating VITALS_ENCRYPTION_KEY:
  1. Set VITALS_ENCRYPTION_KEY="<new_key>,<old_key>" and deploy.
  2. Run:  python -m scripts.reencrypt_health_data --dry-run   (reports counts)
           python -m scripts.reencrypt_health_data             (writes)
  3. When it reports 0 remaining, set VITALS_ENCRYPTION_KEY="<new_key>" and deploy.

Idempotent (re-running rotates already-rotated values harmlessly), works in
batches, and never prints decrypted values. Legacy plaintext found in
patient_vitals (the model falls back to plaintext on read) is encrypted too.
"""

import argparse
import sys
from dataclasses import dataclass

from cryptography.fernet import InvalidToken
from sqlalchemy import text

from app.core.security import encrypt, rotate_ciphertext
from app.db.session import engine


@dataclass(frozen=True)
class EncryptedColumn:
    table: str
    column: str
    plaintext_fallback: bool  # True where the model reads unencrypted legacy values


COLUMNS = [
    EncryptedColumn("patient_vitals", "value", plaintext_fallback=True),
    EncryptedColumn("patient_vitals", "notes", plaintext_fallback=True),
    EncryptedColumn("fallguard_assessments", "ai_summary_encrypted", plaintext_fallback=False),
    EncryptedColumn("fallguard_assessments", "ai_recommendations_encrypted", plaintext_fallback=False),
    EncryptedColumn("fallguard_answers", "answer_encrypted", plaintext_fallback=False),
    EncryptedColumn("care_plans", "diagnosis_encrypted", plaintext_fallback=False),
    EncryptedColumn("care_plans", "allergies_encrypted", plaintext_fallback=False),
    EncryptedColumn("care_plans", "special_instructions_encrypted", plaintext_fallback=False),
    EncryptedColumn("care_plan_goals", "title_encrypted", plaintext_fallback=False),
    EncryptedColumn("care_plan_goals", "target_encrypted", plaintext_fallback=False),
    EncryptedColumn("care_plan_tasks", "title_encrypted", plaintext_fallback=False),
    EncryptedColumn("care_plan_medications", "detail_encrypted", plaintext_fallback=False),
    EncryptedColumn("care_plan_observations", "body_encrypted", plaintext_fallback=False),
]


def _looks_like_fernet_token(value: str) -> bool:
    # Fernet tokens are urlsafe base64 of a version byte 0x80 + timestamp: they start "gAAAAA".
    return value.startswith("gAAAAA")


@dataclass
class Report:
    rotated: int = 0
    encrypted_plaintext: int = 0
    unreadable: int = 0


def reencrypt(dry_run: bool = False, batch_size: int = 500) -> dict[str, Report]:
    results: dict[str, Report] = {}
    for col in COLUMNS:
        report = Report()
        last_id = None
        while True:
            with engine.begin() as conn:
                params = {"limit": batch_size}
                where = f'"{col.column}" IS NOT NULL'
                if last_id is not None:
                    where += " AND id > :last_id"
                    params["last_id"] = last_id
                rows = conn.execute(
                    text(f'SELECT id, "{col.column}" FROM {col.table} WHERE {where} ORDER BY id LIMIT :limit'),
                    params,
                ).fetchall()
                if not rows:
                    break
                for row_id, value in rows:
                    try:
                        new_value = rotate_ciphertext(value)
                        report.rotated += 1
                    except InvalidToken:
                        # A value that looks like a Fernet token but can't be read means a
                        # key is missing: never treat it as plaintext (that would double-encrypt).
                        if not col.plaintext_fallback or _looks_like_fernet_token(value):
                            report.unreadable += 1
                            continue
                        new_value = encrypt(value)
                        report.encrypted_plaintext += 1
                    if not dry_run:
                        conn.execute(
                            text(f'UPDATE {col.table} SET "{col.column}" = :v WHERE id = :id'),
                            {"v": new_value, "id": row_id},
                        )
                last_id = rows[-1][0]
        results[f"{col.table}.{col.column}"] = report
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args(argv)

    results = reencrypt(dry_run=args.dry_run, batch_size=args.batch_size)
    unreadable = 0
    for name, r in results.items():
        print(f"{name}: rotated={r.rotated} encrypted_plaintext={r.encrypted_plaintext} unreadable={r.unreadable}")
        unreadable += r.unreadable
    if args.dry_run:
        print("dry run: nothing written")
    if unreadable:
        print(f"ERROR: {unreadable} values could not be decrypted with any configured key", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
