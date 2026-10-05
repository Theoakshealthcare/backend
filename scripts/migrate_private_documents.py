"""One-off: move caregiver documents out of the public /uploads mount into PRIVATE_UPLOAD_DIR.

Before WP-C, documents were saved as `uploads/caregivers/<id>/documents/<file>` and the whole
`uploads/` tree was served at /uploads, so anyone with the URL could read a caregiver's ID or
licence. Now they live under PRIVATE_UPLOAD_DIR and `caregiver_documents.file_path` is a key
relative to it (`caregivers/<id>/documents/<file>`).

Run inside the api container (or anywhere with the app's env), from the repo root:

    python -m scripts.migrate_private_documents --dry-run   # report only
    python -m scripts.migrate_private_documents             # move files, rewrite file_path

Safe to re-run: rows already migrated are skipped, and rows whose file is missing are reported
and left untouched. A row whose path would escape the legacy uploads directory is never moved.
"""

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

LEGACY_PREFIX = "uploads/"


@dataclass
class MigrationStats:
    moved: int = 0
    missing: int = 0
    skipped: int = 0


def migrate(
    db: Session, legacy_root: Path, private_root: Path, dry_run: bool = False
) -> MigrationStats:
    from app.db.models.caregiver import CaregiverDocument

    legacy_root = Path(legacy_root).resolve()
    private_root = Path(private_root).resolve()
    stats = MigrationStats()

    for doc in db.query(CaregiverDocument).all():
        if not doc.file_path.startswith(LEGACY_PREFIX):
            stats.skipped += 1
            continue

        key = doc.file_path[len(LEGACY_PREFIX):]
        source = (legacy_root / key).resolve()
        target = (private_root / key).resolve()
        if (
            not source.is_relative_to(legacy_root)
            or not target.is_relative_to(private_root)
            or not source.is_file()
        ):
            print(f"MISSING  {doc.id}  {doc.file_path}")
            stats.missing += 1
            continue

        print(f"{'WOULD MOVE' if dry_run else 'MOVE'}  {doc.id}  {doc.file_path} -> {key}")
        stats.moved += 1
        if dry_run:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        doc.file_path = key
        db.commit()

    return stats


def main() -> None:
    from app.core.config import settings
    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report what would move, change nothing")
    parser.add_argument("--legacy-root", default="uploads")
    parser.add_argument("--private-root", default=settings.PRIVATE_UPLOAD_DIR)
    args = parser.parse_args()

    with SessionLocal() as db:
        stats = migrate(db, Path(args.legacy_root), Path(args.private_root), dry_run=args.dry_run)
    print(f"moved={stats.moved} missing={stats.missing} skipped={stats.skipped}")


if __name__ == "__main__":
    main()
