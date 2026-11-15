DESCRIPTION = 'Time-phased net requirements planning with shared components'
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .common import read, save
from .model import analyze
from .schedule import schedule_version
from .store import (BatchConflict, DuplicateBatch, RevisionError, apply_batch,
                    initialize, list_versions, load_report,
                    load_schedule_source)


def run(source):
    data, digest = read(source)
    return {'schema_version': 1, 'source_sha256': digest, 'result': analyze(data)}


def _read_payload(path):
    data, _digest = read(path)
    return data


def _init(args):
    data, digest = read(args.source)
    initialize(args.database, data, digest)
    report = load_report(args.database, 0)
    if args.output:
        save(args.output, report)
    print(json.dumps({'database': str(args.database), 'version': 0,
                      'batch_id': None}, sort_keys=True))
    return 0


def _apply(args):
    payload = _read_payload(args.batch)
    version, replayed = apply_batch(args.database, args.batch_id, args.base_version, payload)
    report = load_report(args.database, version)
    if args.output:
        save(args.output, report)
    print(json.dumps({'database': str(args.database), 'version': version,
                      'batch_id': args.batch_id, 'replayed': replayed}, sort_keys=True))
    return 0


def _report(args):
    report = load_report(args.database, args.version)
    if args.output:
        save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def _versions(args):
    print(json.dumps(list_versions(args.database), sort_keys=True, ensure_ascii=False))
    return 0


def _schedule(args):
    calendar, digest = read(args.calendar)
    source = load_schedule_source(args.database, args.version)
    report = {'schema_version': 1,
              'revision': {'version': source['version'],
                           'head_version': source['head_version']},
              'calendar_sha256': digest,
              'result': schedule_version(source, calendar)}
    if args.output:
        save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command')

    p_init = sub.add_parser('init', help='import the original JSON plan as version 0')
    p_init.add_argument('--source', type=Path, required=True)
    p_init.add_argument('--database', type=Path, required=True)
    p_init.add_argument('--output', type=Path)
    p_init.set_defaults(func=_init)

    p_apply = sub.add_parser('apply', help='apply a signed delta batch as a new revision')
    p_apply.add_argument('--database', type=Path, required=True)
    p_apply.add_argument('--batch', type=Path, required=True)
    p_apply.add_argument('--batch-id', required=True)
    p_apply.add_argument('--base-version', type=int, required=True)
    p_apply.add_argument('--output', type=Path)
    p_apply.set_defaults(func=_apply)

    p_report = sub.add_parser('report', help='read any committed revision')
    p_report.add_argument('--database', type=Path, required=True)
    p_report.add_argument('--version', type=int)
    p_report.add_argument('--output', type=Path)
    p_report.set_defaults(func=_report)

    p_versions = sub.add_parser('versions', help='list committed revisions')
    p_versions.add_argument('--database', type=Path, required=True)
    p_versions.set_defaults(func=_versions)

    p_schedule = sub.add_parser(
        'schedule', help='finite-capacity schedule of one committed revision')
    p_schedule.add_argument('--database', type=Path, required=True)
    p_schedule.add_argument('--calendar', type=Path, required=True)
    p_schedule.add_argument('--version', type=int)
    p_schedule.add_argument('--output', type=Path)
    p_schedule.set_defaults(func=_schedule)

    # Legacy whole-file mode (unchanged contract).
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command is not None:
            return args.func(args)
        if not args.source or not args.output:
            parser.error('--source and --output are required unless using a subcommand')
        if args.source.resolve() == args.output.resolve():
            raise ValueError('input and output must differ')
        report = run(args.source)
        save(args.output, report)
    except (BatchConflict, DuplicateBatch) as error:
        print(str(error), file=sys.stderr)
        return 3
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
