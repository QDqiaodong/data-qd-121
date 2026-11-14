"""SQLite-backed incremental revision store.

Every revision (input rows, batch metadata, recomputed plan snapshots, per-item
deltas and the head pointer) is written inside one ``BEGIN IMMEDIATE``
transaction.  A process killed mid-batch therefore leaves either the previous
head or the complete new head; no half version is ever readable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .common import ident
from .contracts import parse
from .model import analyze, revise_plan

SCHEMA_VERSION = 1
DELTA_BOUND = 10 ** 9
QUANTITY_BOUND = 10 ** 9

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE item_policy (
    item_id TEXT PRIMARY KEY,
    lead_time INTEGER NOT NULL,
    lot_size INTEGER NOT NULL,
    on_hand INTEGER NOT NULL,
    safety_stock INTEGER NOT NULL,
    topo_index INTEGER NOT NULL
);
CREATE TABLE bom_edge (
    parent TEXT NOT NULL,
    component TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (parent, component)
);
CREATE TABLE input_cell (
    kind TEXT NOT NULL CHECK (kind IN ('demand', 'receipts')),
    item_id TEXT NOT NULL,
    period INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (kind, item_id, period)
);
CREATE TABLE version (
    version INTEGER PRIMARY KEY,
    created_by_batch TEXT,
    parent_version INTEGER,
    base_version INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE batch (
    batch_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL UNIQUE,
    base_version INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    canonical_payload TEXT NOT NULL
);
CREATE TABLE head (
    only_id INTEGER PRIMARY KEY CHECK (only_id = 1),
    version INTEGER NOT NULL
);
CREATE TABLE plan_snapshot (
    version INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    series_json TEXT NOT NULL,
    releases_json TEXT NOT NULL,
    pegging_json TEXT NOT NULL,
    past_due_json TEXT NOT NULL,
    PRIMARY KEY (version, item_id)
);
CREATE TABLE plan_diff (
    version INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    period INTEGER NOT NULL,
    field TEXT NOT NULL,
    delta INTEGER NOT NULL,
    PRIMARY KEY (version, item_id, kind, period, field)
);
CREATE TABLE recomputed_item (
    version INTEGER NOT NULL,
    topo_index INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    PRIMARY KEY (version, item_id)
);
"""


class RevisionError(ValueError):
    """Rejected batch: stale base, duplicate id, negative result, bad payload."""


class BatchConflict(RevisionError):
    """Another writer committed first; the base version is no longer the head."""


class DuplicateBatch(RevisionError):
    """Batch id already exists (with differing or identical content)."""


def connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 15000')
    return conn


def _begin_immediate(conn, attempts=10, delay=0.05):
    """Acquire the write lock, treating lock exhaustion as a batch conflict.

    busy_timeout normally serializes contenders; the bounded retry covers the
    residual case so callers get the semantic conflict (exit 3) rather than a
    raw OperationalError (exit 2).
    """
    last_error = None
    for _ in range(attempts):
        try:
            conn.execute('BEGIN IMMEDIATE')
            return
        except sqlite3.OperationalError as error:
            last_error = error
            time.sleep(delay)
    raise BatchConflict('database locked by another writer: ' + str(last_error))


def _discard_uncommitted_database(path):
    """Remove a failed init's empty database, never a concurrently committed one."""
    if not path.exists():
        return
    try:
        probe = connect(path)
        try:
            committed = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'head'").fetchone()
        finally:
            probe.close()
    except sqlite3.DatabaseError:
        committed = None
    if not committed:
        path.unlink(missing_ok=True)


def initialize(db_path, data, source_sha256):
    """Import the original JSON contract as version 0 in a single transaction."""
    path = Path(db_path)
    if path.exists() and path.stat().st_size > 0:
        raise RevisionError('database already exists: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    periods, items, bom, demand, receipts, order = parse(data)
    result = analyze(data)
    conn = connect(path)
    try:
        _begin_immediate(conn)
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'head'").fetchone():
            raise RevisionError('database already exists: ' + str(path))
        # Individual execute() calls keep the DDL inside this single
        # transaction; executescript() would issue an implicit COMMIT first.
        for statement in SCHEMA.split(';'):
            if statement.strip():
                conn.execute(statement)
        conn.executemany(
            'INSERT INTO meta(key, value) VALUES (?, ?)',
            [('schema_version', str(SCHEMA_VERSION)),
             ('periods', str(periods)),
             ('source_sha256', source_sha256),
             ('head_version', '0')])
        policy_rows = [(item, items[item]['lead_time'], items[item]['lot_size'],
                        items[item]['on_hand'], items[item]['safety_stock'],
                        order.index(item)) for item in order]
        conn.executemany(
            'INSERT INTO item_policy VALUES (?, ?, ?, ?, ?, ?)', policy_rows)
        edge_rows = [(p, c, q) for p in order for c, q in bom[p]]
        conn.executemany('INSERT INTO bom_edge VALUES (?, ?, ?)', edge_rows)
        cell_rows = []
        for kind, matrix in (('demand', demand), ('receipts', receipts)):
            for item in order:
                for t, value in enumerate(matrix[item]):
                    if value:
                        cell_rows.append((kind, item, t, value))
        conn.executemany('INSERT INTO input_cell VALUES (?, ?, ?, ?)', cell_rows)
        conn.execute(
            'INSERT INTO version VALUES (0, ?, NULL, NULL, ?)',
            (None, _now()))
        conn.execute('INSERT INTO head(only_id, version) VALUES (1, 0)')
        pegging_map = {item: [] for item in order}
        for peg in result['component_pegging']:
            pegging_map[peg['parent']].append(peg)
        past_due_map = {item: [] for item in order}
        for entry in result['past_due_releases']:
            past_due_map[entry['item']].append(entry)
        _persist_snapshot(conn, 0, order, result['plans'], pegging_map, past_due_map)
        conn.executemany('INSERT INTO recomputed_item VALUES (?, ?, ?)',
                         [(0, index, item) for index, item in enumerate(order)])
        conn.commit()
    except BaseException:
        conn.rollback()
        conn.close()
        _discard_uncommitted_database(path)
        raise
    conn.close()
    return 0


def apply_batch(db_path, batch_id, base_version, payload, crash_hook=None):
    """Merge signed demand/receipts deltas and create one new revision.

    Same batch id with identical content returns the original version; same id
    with different content is rejected.  A new batch must base on the current
    head.  All rows commit in one transaction so only one competing writer wins.
    """
    try:
        batch_id = ident(batch_id)
    except ValueError as error:
        raise RevisionError(str(error)) from error
    base_version = _as_version(base_version)
    changes = _validate_payload(payload)
    canonical = _canonical(changes)
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()

    conn = connect(db_path)
    try:
        # Idempotency check before taking the write lock: identical replays must
        # return the stored version even while another batch is in flight.
        existing = conn.execute(
            'SELECT version, payload_sha256 FROM batch WHERE batch_id = ?',
            (batch_id,)).fetchone()
        if existing is not None:
            if existing['payload_sha256'] != digest:
                raise DuplicateBatch('batch id already used with different content: ' + batch_id)
            return existing['version'], True

        _begin_immediate(conn)
        # Re-check under the write lock to close the race with a concurrent apply.
        existing = conn.execute(
            'SELECT version, payload_sha256 FROM batch WHERE batch_id = ?',
            (batch_id,)).fetchone()
        if existing is not None:
            if existing['payload_sha256'] != digest:
                raise DuplicateBatch('batch id already used with different content: ' + batch_id)
            conn.commit()
            return existing['version'], True
        head = conn.execute('SELECT version FROM head WHERE only_id = 1').fetchone()['version']
        if base_version != head:
            raise BatchConflict(
                f'base version {base_version} is not current head {head}')

        periods, items, bom, order = _load_static(conn)
        demand, receipts = _load_inputs(conn, items, periods)
        merged = _merge_changes(demand, receipts, changes)
        if not _has_effect(merged):
            raise RevisionError('batch deltas net to zero; nothing to revise')
        head_plans = _load_head_plans(conn, head, items, periods)

        plans, diffs, new_pegging, new_past_due, recomputed = revise_plan(
            periods, items, bom, order, demand, receipts, head_plans,
            merged['demand_delta'], merged['receipt_delta'])

        new_version = head + 1
        conn.execute('INSERT INTO version VALUES (?, ?, ?, ?, ?)',
                     (new_version, batch_id, head, base_version, _now()))
        conn.execute('INSERT INTO batch VALUES (?, ?, ?, ?, ?)',
                     (batch_id, new_version, base_version, digest, canonical))
        for kind, matrix_delta in (('demand', merged['demand_delta']),
                                   ('receipts', merged['receipt_delta'])):
            for item, period_values in matrix_delta.items():
                for period, delta in period_values.items():
                    if not delta:
                        continue
                    current = conn.execute(
                        'SELECT quantity FROM input_cell WHERE kind = ? AND item_id = ? AND period = ?',
                        (kind, item, period)).fetchone()
                    quantity = (current['quantity'] if current else 0) + delta
                    if current is None:
                        conn.execute('INSERT INTO input_cell VALUES (?, ?, ?, ?)',
                                     (kind, item, period, quantity))
                    elif quantity == 0:
                        conn.execute(
                            'DELETE FROM input_cell WHERE kind = ? AND item_id = ? AND period = ?',
                            (kind, item, period))
                    else:
                        conn.execute(
                            'UPDATE input_cell SET quantity = ? WHERE kind = ? AND item_id = ? AND period = ?',
                            (quantity, kind, item, period))
        snapshots = {item: plans[item] for item in recomputed}
        _persist_snapshot(
            conn, new_version, order,
            [{'item': item, 'periods': snapshots[item]['periods'],
              'planned_releases': snapshots[item]['planned_releases']}
             for item in recomputed],
            {item: new_pegging[item] for item in recomputed},
            {item: new_past_due[item] for item in recomputed})
        diff_rows = []
        for item in recomputed:
            for entry in diffs[item]['series']:
                period = entry['period']
                for field, delta in entry.items():
                    if field == 'period':
                        continue
                    diff_rows.append((new_version, item, 'series', period, field, delta))
            for entry in diffs[item]['planned_releases']:
                diff_rows.append((new_version, item, 'planned_releases',
                                  entry['period'], 'quantity', entry['delta']))
        conn.executemany('INSERT INTO plan_diff VALUES (?, ?, ?, ?, ?, ?)', diff_rows)
        conn.executemany(
            'INSERT INTO recomputed_item VALUES (?, ?, ?)',
            [(new_version, order.index(item), item) for item in recomputed])
        conn.execute('UPDATE head SET version = ? WHERE only_id = 1', (new_version,))
        # Test seam: simulate process termination immediately before commit.
        if crash_hook is not None:
            crash_hook(conn)
        conn.commit()
        return new_version, False
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def load_report(db_path, version=None):
    """Read any committed version; defaults to the current head."""
    conn = connect(db_path)
    try:
        head = conn.execute('SELECT version FROM head WHERE only_id = 1').fetchone()['version']
        if version is None:
            version = head
        version = _as_version(version)
        row = conn.execute('SELECT * FROM version WHERE version = ?', (version,)).fetchone()
        if row is None:
            raise RevisionError(f'unknown version {version}')
        periods, items, bom, order = _load_static(conn)
        plans = _load_head_plans(conn, version, items, periods)
        pegging, past_due = [], []
        for item in order:
            pegging.extend(plans[item]['component_pegging'])
            past_due.extend(plans[item]['past_due_releases'])
        outside = [p for p in pegging if p['requirement_period'] < 0]
        diffs = _load_diffs(conn, version)
        recomputed = [r['item_id'] for r in conn.execute(
            'SELECT item_id FROM recomputed_item WHERE version = ? ORDER BY topo_index',
            (version,))]
        meta = {r['key']: r['value']
                for r in conn.execute('SELECT key, value FROM meta')}
        result = {
            'planning_order': order,
            'plans': [{'item': item,
                       'periods': plans[item]['periods'],
                       'planned_releases': plans[item]['planned_releases']}
                      for item in order],
            'component_pegging': pegging,
            'past_due_releases': past_due,
            'outside_horizon_component_requirements': outside,
            'feasible_within_horizon': not past_due,
            'supply_assumption': 'Planned receipts are assumed available even if their release is flagged past due.'}
        return {
            'schema_version': SCHEMA_VERSION,
            'source_sha256': meta['source_sha256'],
            'revision': {
                'version': version,
                'head_version': head,
                'batch_id': row['created_by_batch'],
                'base_version': row['base_version'],
                'parent_version': row['parent_version'],
                'created_at': row['created_at']},
            'result': result,
            'plan_diffs': diffs,
            'recomputed_items': recomputed}
    finally:
        conn.close()


def list_versions(db_path):
    conn = connect(db_path)
    try:
        return [dict(r) for r in conn.execute(
            'SELECT v.version, v.created_by_batch, v.base_version, v.parent_version, v.created_at, '
            'b.payload_sha256 FROM version v LEFT JOIN batch b ON b.version = v.version '
            'ORDER BY v.version')]
    finally:
        conn.close()


# ---------------------------------------------------------------- internals

def _now():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _as_version(value):
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise RevisionError('version must be a non-negative integer')
    return value


def _validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) - {'demand', 'receipts'}:
        raise RevisionError("payload object may only contain 'demand' and 'receipts'")
    changes = {'demand': [], 'receipts': []}
    for kind in changes:
        rows = payload.get(kind, [])
        if not isinstance(rows, list) or len(rows) > 20000:
            raise RevisionError(kind + ' must be a bounded array')
        for entry in rows:
            if not isinstance(entry, dict) or set(entry) != {'item', 'period', 'delta'}:
                raise RevisionError(kind + ' rows require item, period, delta')
            try:
                item = ident(entry['item'])
            except ValueError as error:
                raise RevisionError(str(error)) from error
            period = entry['period']
            delta = entry['delta']
            if type(period) is not int or isinstance(period, bool) or not 0 <= period <= 99:
                raise RevisionError('period outside 0..99')
            if type(delta) is not int or isinstance(delta, bool) or not -DELTA_BOUND <= delta <= DELTA_BOUND:
                raise RevisionError('delta must be a signed integer within +/-10^9')
            changes[kind].append((item, period, delta))
    if not changes['demand'] and not changes['receipts']:
        raise RevisionError('batch contains no changes')
    return changes


def _canonical(changes):
    compact = {kind: sorted(changes[kind]) for kind in changes}
    return json.dumps(compact, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _merge_changes(demand, receipts, changes):
    """Collapse same-key deltas and enforce non-negative merged quantities."""
    demand_delta, receipt_delta = {}, {}
    for kind, matrix, target in (('demand', demand, demand_delta),
                                 ('receipts', receipts, receipt_delta)):
        for item, period, delta in changes[kind]:
            if item not in matrix:
                raise RevisionError('unknown item: ' + item)
            if period >= len(matrix[item]):
                raise RevisionError(
                    f'period {period} outside horizon 0..{len(matrix[item]) - 1}')
            bucket = target.setdefault(item, {})
            bucket[period] = bucket.get(period, 0) + delta
        for item, period_values in target.items():
            for period, delta in period_values.items():
                merged = matrix[item][period] + delta
                if not 0 <= merged <= QUANTITY_BOUND:
                    raise RevisionError(
                        f'{kind} for {item} period {period} would be {merged}; must be 0..10^9')
    return {'demand_delta': demand_delta, 'receipt_delta': receipt_delta}


def _has_effect(merged):
    for matrix_delta in (merged['demand_delta'], merged['receipt_delta']):
        for period_values in matrix_delta.values():
            if any(period_values.values()):
                return True
    return False


def _load_static(conn):
    meta = {r['key']: r['value'] for r in conn.execute('SELECT key, value FROM meta')}
    periods = int(meta['periods'])
    items, order = {}, []
    for r in conn.execute('SELECT * FROM item_policy ORDER BY topo_index'):
        order.append(r['item_id'])
        items[r['item_id']] = {'lead_time': r['lead_time'], 'lot_size': r['lot_size'],
                               'on_hand': r['on_hand'], 'safety_stock': r['safety_stock']}
    bom = {item: [] for item in order}
    for r in conn.execute('SELECT parent, component, quantity FROM bom_edge ORDER BY parent, component'):
        bom[r['parent']].append((r['component'], r['quantity']))
    return periods, items, bom, order


def _load_inputs(conn, items, periods):
    demand = {item: [0] * periods for item in items}
    receipts = {item: [0] * periods for item in items}
    for r in conn.execute('SELECT * FROM input_cell'):
        target = demand if r['kind'] == 'demand' else receipts
        target[r['item_id']][r['period']] = r['quantity']
    return demand, receipts


def _load_head_plans(conn, version, items, periods):
    """Reconstruct full plans at ``version`` from the latest snapshot per item."""
    plans = {}
    rows = conn.execute(
        'SELECT ps.* FROM plan_snapshot ps JOIN ('
        ' SELECT item_id, MAX(version) AS mv FROM plan_snapshot WHERE version <= ? GROUP BY item_id'
        ') latest ON latest.item_id = ps.item_id AND latest.mv = ps.version',
        (version,)).fetchall()
    found = set()
    for r in rows:
        found.add(r['item_id'])
        plans[r['item_id']] = {
            'item': r['item_id'],
            'periods': json.loads(r['series_json']),
            'planned_releases': json.loads(r['releases_json']),
            'component_pegging': json.loads(r['pegging_json']),
            'past_due_releases': json.loads(r['past_due_json'])}
    missing = set(items) - found
    if missing:
        raise RevisionError('corrupt store: missing plan snapshots for ' + ', '.join(sorted(missing)))
    return plans


def _persist_snapshot(conn, version, order, plans, pegging_map, past_due_map):
    rows = []
    for plan in plans:
        item = plan['item']
        rows.append((version, item,
                     json.dumps(plan['periods'], separators=(',', ':'), ensure_ascii=False),
                     json.dumps(plan['planned_releases'], separators=(',', ':'), ensure_ascii=False),
                     json.dumps(pegging_map[item], separators=(',', ':'), ensure_ascii=False),
                     json.dumps(past_due_map[item], separators=(',', ':'), ensure_ascii=False)))
    conn.executemany('INSERT INTO plan_snapshot VALUES (?, ?, ?, ?, ?, ?)', rows)


def _load_diffs(conn, version):
    diffs = {}
    for r in conn.execute('SELECT * FROM plan_diff WHERE version = ? ORDER BY item_id, kind, period, field',
                          (version,)):
        item = diffs.setdefault(r['item_id'], {'series': [], 'planned_releases': []})
        if r['kind'] == 'series':
            bucket = item['series']
            entry = next((e for e in bucket if e['period'] == r['period']), None)
            if entry is None:
                entry = {'period': r['period']}
                bucket.append(entry)
            entry[r['field']] = r['delta']
        else:
            item['planned_releases'].append({'period': r['period'], 'delta': r['delta']})
    return diffs
