"""Merge the per-shard duration files from a CI run into the committed .test_durations.

    gh run download <run id> -R Theoakshealthcare/backend -p 'test-durations-*' -D /tmp/durations
    python3 scripts/merge_test_durations.py /tmp/durations/*/durations-*.json

Each shard only measured the tests it ran, so the files are disjoint: the union is the whole suite.
"""

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / ".test_durations"


def main(paths):
    if not paths:
        sys.exit(__doc__)
    merged = {}
    for path in paths:
        merged.update(json.loads(Path(path).read_text()))
    OUT.write_text(json.dumps(dict(sorted(merged.items())), indent=2) + "\n")
    print(f"{len(merged)} tests from {len(paths)} files -> {OUT.name}")


if __name__ == "__main__":
    main(sys.argv[1:])
