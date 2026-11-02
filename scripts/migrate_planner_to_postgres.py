"""Planner-only migration CLI. Default: read-only SQLite review, no PG connection."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.planner.backend.sqlite_transfer import inspect_source, inspect_target, transfer, verify_transfer


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Consistent Planner SQLite v8 snapshot')
    parser.add_argument('--check-target', action='store_true', help='Also inspect destination in a read-only transaction')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--apply', action='store_true', help='Create and copy only into an empty planner schema')
    modes.add_argument('--verify', action='store_true', help='Read-only content verification after transfer or restore')
    parser.add_argument('--expected-source-sha256', help='source_data_sha256 from the reviewed dry-run')
    args = parser.parse_args(argv)
    # Intentionally not DATABASE_URL or the runtime Planner URL: no accidental
    # switch, credentials in command arguments, or implicit core DB selection.
    destination = os.getenv('PLANNER_MIGRATION_DATABASE_URL', '').strip()
    if (args.apply or args.verify or args.check_target) and not destination:
        parser.error('Set PLANNER_MIGRATION_DATABASE_URL explicitly; DATABASE_URL is not used.')
    if args.check_target and (args.apply or args.verify):
        parser.error('--check-target is only for the pre-transfer dry-run.')
    if args.apply and not args.expected_source_sha256:
        parser.error('--apply requires --expected-source-sha256 from a dry-run.')
    try:
        if args.apply:
            report = transfer(args.source, destination, expected_source_sha256=args.expected_source_sha256)
        elif args.verify:
            report = verify_transfer(args.source, destination)
        else:
            report = inspect_source(args.source)
            if args.check_target:
                report.update(inspect_target(destination))
    except Exception as error:
        # Driver exception strings can contain connection details or source rows.
        from apps.planner.backend.sqlite_transfer import TransferError
        message = str(error) if isinstance(error, TransferError) else type(error).__name__
        print('Planner transfer failed: ' + message, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
