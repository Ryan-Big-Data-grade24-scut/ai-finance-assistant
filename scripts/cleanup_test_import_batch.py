from __future__ import annotations

import argparse
import json

from backend.ai.test_batch_cleanup import (
    DEFAULT_BATCH_ID,
    DEFAULT_EXPECTED_COUNT,
    DEFAULT_EXPECTED_IDS,
    DEFAULT_FILENAME,
    apply_cleanup,
    inspect_batch,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely hide the known synthetic test import batch using deletion tombstones."
    )
    parser.add_argument("--batch-id", default=DEFAULT_BATCH_ID)
    parser.add_argument("--apply", action="store_true", help="Actually create tombstones and sync the live book.")
    parser.add_argument("--confirm-batch", default="", help="Must exactly equal the batch ID when --apply is used.")
    args = parser.parse_args()

    preview = inspect_batch(
        args.batch_id,
        expected_filename=DEFAULT_FILENAME,
        expected_ids=DEFAULT_EXPECTED_IDS,
        expected_count=DEFAULT_EXPECTED_COUNT,
    )
    if not args.apply:
        print("DRY RUN ONLY — no data has been changed.")
        print(json.dumps(preview, ensure_ascii=False, indent=2, default=str))
        print("\nTo apply, run:")
        print(
            f"python scripts/cleanup_test_import_batch.py --apply "
            f"--confirm-batch {args.batch_id}"
        )
        return 0

    if args.confirm_batch != args.batch_id:
        parser.error("--apply requires --confirm-batch exactly equal to --batch-id.")

    result = apply_cleanup(
        args.batch_id,
        expected_filename=DEFAULT_FILENAME,
        expected_ids=DEFAULT_EXPECTED_IDS,
        expected_count=DEFAULT_EXPECTED_COUNT,
    )
    print("CLEANUP COMPLETE")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
