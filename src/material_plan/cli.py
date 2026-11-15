DESCRIPTION = 'Time-phased net requirements planning with shared components'
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from .common import read, save
from .model import analyze
from .schedule import schedule_version
from .store import (BatchConflict, DuplicateBatch, DuplicateOrder,
                    ReservationRejected, apply_batch,
                    availability as load_availability, initialize,
                    list_versions, load_report, load_schedule_source,
                    release_order, reserve_order)
from .trace import RecallResult, build as build_trace


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


def _reserve(args):
    payload = _read_payload(args.order)
    result = reserve_order(args.database, payload)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def _release(args):
    result = release_order(args.database, args.order_id)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


def _availability(args):
    report = load_availability(args.database)
    if args.output:
        save(args.output, report)
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
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


def _build(args):
    summary = build_trace(args.trace_database, args.plan_database, args.events)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False))
    return 0


def _write_recall_stream(path, result):
    """Write the recall report incrementally so large lot sets are not buffered."""
    dest = Path(path)
    if dest.is_symlink():
        raise ValueError('output cannot be a symlink')
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=dest.parent, delete=False) as stream:
            temp = stream.name
            stream.write('{\n')
            header = result.header()
            stream.write('"header": ')
            json.dump(header, stream, sort_keys=True, ensure_ascii=False,
                      allow_nan=False)
            stream.write(',\n"remaining_lots": [\n')
            first = True
            for row in result.rows():
                if not first:
                    stream.write(',\n')
                first = False
                json.dump(row, stream, sort_keys=True, ensure_ascii=False,
                          allow_nan=False)
            if first:
                stream.write(']\n')
            else:
                stream.write('\n]\n')
            stream.write('}\n')
        os.replace(temp, dest)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def _recall(args):
    with RecallResult(args.trace_database, args.lot) as result:
        if args.output:
            _write_recall_stream(args.output, result)
            print(json.dumps(
                {'database': str(args.trace_database), 'lot': args.lot,
                 'output': str(args.output),
                 'total_attributed': result.header()['total_attributed']},
                sort_keys=True, ensure_ascii=False, allow_nan=False))
        else:
            print(json.dumps(_recall_payload(result), sort_keys=True,
                             ensure_ascii=False, allow_nan=False))
    return 0


def _recall_payload(result):
    return {'header': result.header(),
            'remaining_lots': list(result.rows())}


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

    p_reserve = sub.add_parser(
        'reserve', help='reserve leaf supply for one multi-line sales order')
    p_reserve.add_argument('--database', type=Path, required=True)
    p_reserve.add_argument('--order', type=Path, required=True)
    p_reserve.set_defaults(func=_reserve)

    p_release = sub.add_parser(
        'release', help='release one sales order and re-allocate the rest')
    p_release.add_argument('--database', type=Path, required=True)
    p_release.add_argument('--order-id', type=int, required=True)
    p_release.set_defaults(func=_release)

    p_availability = sub.add_parser(
        'availability', help='per-supply remaining availability of leaf items')
    p_availability.add_argument('--database', type=Path, required=True)
    p_availability.add_argument('--output', type=Path)
    p_availability.set_defaults(func=_availability)

    p_schedule = sub.add_parser(
        'schedule', help='finite-capacity schedule of one committed revision')
    p_schedule.add_argument('--database', type=Path, required=True)
    p_schedule.add_argument('--calendar', type=Path, required=True)
    p_schedule.add_argument('--version', type=int)
    p_schedule.add_argument('--output', type=Path)
    p_schedule.set_defaults(func=_schedule)

    p_build = sub.add_parser(
        'build', help='import out-of-order lot events into a trace database')
    p_build.add_argument('--plan-database', type=Path, required=True)
    p_build.add_argument('--events', type=Path, required=True)
    p_build.add_argument('--trace-database', type=Path, required=True)
    p_build.set_defaults(func=_build)

    p_recall = sub.add_parser(
        'recall', help='exact-fraction lot recall from a trace database')
    p_recall.add_argument('--trace-database', type=Path, required=True)
    p_recall.add_argument('--lot', required=True)
    p_recall.add_argument('--output', type=Path)
    p_recall.set_defaults(func=_recall)

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
    except (BatchConflict, DuplicateBatch, DuplicateOrder,
            ReservationRejected) as error:
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
