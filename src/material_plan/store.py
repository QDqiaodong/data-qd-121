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
from .model import (CommitmentInfeasible, _ON_HAND_KIND, _RECEIPT_KIND,
                    allocate_commitments, analyze, build_supply_points,
                    fifo_version_consumption, leaf_gross_requirements,
                    leaf_items, revise_plan)

SCHEMA_VERSION = 2
DELTA_BOUND = 10 ** 9
QUANTITY_BOUND = 10 ** 9
MAX_ORDER_LINES = 50
MAX_CANDIDATES = 50
MAX_ACTIVE_ORDERS = 200
MAX_ACTIVE_LINES = 1000
LINE_NO_BOUND = 10 ** 6

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

# Schema v2 adds leaf-item sales commitments on top of the v1 revision store.
# Live rows (sales_order/sales_order_line/sales_order_candidate/sales_allocation)
# describe the current active state; sales_allocation_history keeps every
# committed allocation generation as an append-only audit trail.
SCHEMA_V2 = """
CREATE TABLE sales_order (
    order_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('active', 'released')),
    payload_sha256 TEXT NOT NULL,
    canonical_payload TEXT NOT NULL,
    created_version INTEGER NOT NULL,
    released_version INTEGER,
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE TABLE sales_order_line (
    order_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    due_period INTEGER NOT NULL,
    PRIMARY KEY (order_id, line_no)
);
CREATE TABLE sales_order_candidate (
    order_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    position INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    PRIMARY KEY (order_id, line_no, position)
);
CREATE TABLE sales_allocation (
    order_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    supply_period INTEGER NOT NULL,
    supply_kind TEXT NOT NULL CHECK (supply_kind IN ('on_hand', 'scheduled_receipt')),
    quantity INTEGER NOT NULL,
    PRIMARY KEY (order_id, line_no, item_id, supply_period, supply_kind)
);
CREATE TABLE sales_allocation_history (
    generation INTEGER NOT NULL,
    plan_version INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    supply_period INTEGER NOT NULL,
    supply_kind TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (generation, order_id, line_no, item_id, supply_period, supply_kind)
);
"""


class RevisionError(ValueError):
    """Rejected batch: stale base, duplicate id, negative result, bad payload."""


class BatchConflict(RevisionError):
    """Another writer committed first; the base version is no longer the head."""


class DuplicateBatch(RevisionError):
    """Batch id already exists (with differing or identical content)."""


class DuplicateOrder(RevisionError):
    """Order id already exists with different content or was released before."""


class ReservationRejected(RevisionError):
    """The order is well-formed but the active commitments cannot all be met."""


def connect(db_path, readonly=False):
    if readonly:
        # mode=ro never creates the file and rejects every write, which is
        # what the scheduling reader relies on.
        conn = sqlite3.connect(
            'file:' + str(Path(db_path).absolute()) + '?mode=ro', uri=True)
    else:
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
        for statement in (SCHEMA + SCHEMA_V2).split(';'):
            if statement.strip():
                conn.execute(statement)
        conn.executemany(
            'INSERT INTO meta(key, value) VALUES (?, ?)',
            [('schema_version', str(SCHEMA_VERSION)),
             ('periods', str(periods)),
             ('source_sha256', source_sha256),
             ('head_version', '0'),
             ('allocation_generation', '0')])
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
        # Commitments are re-allocated against the post-batch supply/demand in
        # the same transaction: if any active promise can no longer be kept the
        # whole revision is rejected and none of the rows above is ever visible.
        _ensure_commitment_schema(conn)
        post_demand = {item: [demand[item][t] + merged['demand_delta'].get(item, {}).get(t, 0)
                              for t in range(periods)] for item in items}
        post_receipts = {item: [receipts[item][t] + merged['receipt_delta'].get(item, {}).get(t, 0)
                                for t in range(periods)] for item in items}
        _recompute_commitments(conn, periods, items, bom, order,
                               post_demand, post_receipts, new_version)
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


def load_schedule_source(db_path, version=None):
    """Read-only view of one committed version for finite-capacity scheduling.

    Returns the static data (periods, policies, BOM, planning order), the
    independent demand and scheduled receipts exactly as committed at
    ``version`` (later batches are unwound from the current cells), and the
    per-item planned receipts of that version's plan.  The database is opened
    read-only and never modified.
    """
    conn = connect(db_path, readonly=True)
    try:
        head = conn.execute('SELECT version FROM head WHERE only_id = 1').fetchone()
        if head is None:
            raise RevisionError('not an initialized plan database')
        head = head['version']
        if version is None:
            version = head
        version = _as_version(version)
        if conn.execute(
                'SELECT 1 FROM version WHERE version = ?', (version,)).fetchone() is None:
            raise RevisionError(f'unknown version {version}')
        periods, items, bom, order = _load_static(conn)
        demand, receipts = _load_inputs(conn, items, periods)
        later = conn.execute(
            'SELECT canonical_payload FROM batch WHERE version > ? '
            'ORDER BY version DESC', (version,)).fetchall()
        for row in later:
            payload = json.loads(row['canonical_payload'])
            for kind, target in (('demand', demand), ('receipts', receipts)):
                for item, period, delta in payload[kind]:
                    target[item][period] -= delta
        plans = _load_head_plans(conn, version, items, periods)
        planned = {item: [0] * periods for item in items}
        for item in order:
            for row in plans[item]['periods']:
                planned[item][row['period']] = row['planned_receipt']
        return {'version': version, 'head_version': head, 'periods': periods,
                'items': items, 'bom': bom, 'order': order, 'demand': demand,
                'receipts': receipts, 'planned_receipts': planned}
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


# ---------------------------------------------------------------------------
# Leaf-item sales commitments (schema v2)
# ---------------------------------------------------------------------------

def _ensure_commitment_schema(conn):
    """Create the v2 commitment tables inside the open write transaction.

    Fresh ``initialize`` databases already carry them; a pre-v2 database is
    migrated lazily the first time a commitment-aware write runs, so the
    migration is atomic with the write that triggered it.
    """
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sales_order'").fetchone()
    if present:
        return
    for statement in SCHEMA_V2.split(';'):
        if statement.strip():
            conn.execute(statement)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('allocation_generation', '0')")
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                 (str(SCHEMA_VERSION),))


def _validate_order_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {'order_id', 'lines'}:
        raise RevisionError("order payload must contain exactly 'order_id' and 'lines'")
    order_id = payload['order_id']
    if type(order_id) is not int or isinstance(order_id, bool) or not 0 <= order_id <= 10 ** 12:
        raise RevisionError('order_id must be a non-negative integer')
    raw_lines = payload['lines']
    if not isinstance(raw_lines, list) or not raw_lines or len(raw_lines) > MAX_ORDER_LINES:
        raise RevisionError(f'lines must be a non-empty array of at most {MAX_ORDER_LINES}')
    lines, seen_lines = [], set()
    for entry in raw_lines:
        if not isinstance(entry, dict) or set(entry) != {'line_no', 'candidates', 'quantity', 'due_period'}:
            raise RevisionError(
                'order lines require line_no, candidates, quantity, due_period')
        line_no = entry['line_no']
        if type(line_no) is not int or isinstance(line_no, bool) or not 0 <= line_no <= LINE_NO_BOUND:
            raise RevisionError('line_no must be a non-negative integer')
        if line_no in seen_lines:
            raise RevisionError(f'duplicate line_no {line_no}')
        seen_lines.add(line_no)
        quantity = entry['quantity']
        if type(quantity) is not int or isinstance(quantity, bool) or not 1 <= quantity <= QUANTITY_BOUND:
            raise RevisionError('line quantity must be an integer in 1..10^9')
        due_period = entry['due_period']
        if type(due_period) is not int or isinstance(due_period, bool) or due_period < 0:
            raise RevisionError('due_period must be a non-negative integer')
        candidates = entry['candidates']
        if (not isinstance(candidates, list) or not candidates
                or len(candidates) > MAX_CANDIDATES):
            raise RevisionError(
                f'candidates must be a non-empty array of at most {MAX_CANDIDATES}')
        clean = []
        for item in candidates:
            try:
                clean.append(ident(item))
            except ValueError as error:
                raise RevisionError(str(error)) from error
        if len(set(clean)) != len(clean):
            raise RevisionError('duplicate candidate item in line ' + str(line_no))
        lines.append({'line_no': line_no, 'candidates': tuple(sorted(clean)),
                      'quantity': quantity, 'due_period': due_period})
    canonical = json.dumps(
        {'order_id': order_id,
         'lines': [{'line_no': line['line_no'],
                    'candidates': list(line['candidates']),
                    'quantity': line['quantity'],
                    'due_period': line['due_period']}
                   for line in sorted(lines, key=lambda line: line['line_no'])]},
        sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    return order_id, lines, canonical, digest


def _check_order_static(lines, periods, leaf_set):
    for line in lines:
        if line['due_period'] >= periods:
            raise RevisionError(
                f"due_period {line['due_period']} outside horizon 0..{periods - 1}")
        unknown = sorted(set(line['candidates']) - leaf_set)
        if unknown:
            raise RevisionError(
                'candidates must be leaf (purchased) items; unknown or non-leaf: '
                + ', '.join(unknown))


def _load_active_orders(conn):
    orders = {}
    rows = conn.execute(
        'SELECT o.order_id, l.line_no, l.quantity, l.due_period, c.item_id '
        'FROM sales_order o JOIN sales_order_line l ON l.order_id = o.order_id '
        'JOIN sales_order_candidate c ON c.order_id = l.order_id AND c.line_no = l.line_no '
        "WHERE o.status = 'active' ORDER BY o.order_id, l.line_no, c.position")
    for row in rows:
        order = orders.setdefault(row['order_id'],
                                  {'order_id': row['order_id'], 'lines': {}})
        line = order['lines'].setdefault(
            row['line_no'], {'line_no': row['line_no'], 'quantity': row['quantity'],
                             'due_period': row['due_period'], 'candidates': []})
        line['candidates'].append(row['item_id'])
    result = []
    for order_id in sorted(orders):
        order = orders[order_id]
        order['lines'] = [order['lines'][key] for key in sorted(order['lines'])]
        result.append(order)
    return result


def _next_generation(conn):
    value = conn.execute(
        "SELECT value FROM meta WHERE key = 'allocation_generation'").fetchone()
    return int(value['value']) + 1 if value is not None else 1


def _bump_generation(conn, generation):
    conn.execute("UPDATE meta SET value = ? WHERE key = 'allocation_generation'",
                 (str(generation),))


def _persist_allocation(conn, allocations, generation, plan_version):
    conn.execute('DELETE FROM sales_allocation')
    if allocations:
        conn.executemany(
            'INSERT INTO sales_allocation VALUES (?, ?, ?, ?, ?, ?)',
            [(row['order_id'], row['line_no'], row['item'],
              row['supply_period'], row['supply_kind'], row['quantity'])
             for row in allocations])
        conn.executemany(
            'INSERT INTO sales_allocation_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [(generation, plan_version, row['order_id'], row['line_no'], row['item'],
              row['supply_period'], row['supply_kind'], row['quantity'])
             for row in allocations])


def _recompute_commitments(conn, periods, items, bom, order, demand, receipts,
                           plan_version):
    """Full joint re-allocation of every active order under current supply.

    Allocator state never carries across revisions: the FIFO version step and
    the joint search run from scratch on every call, which keeps the result
    directly auditable against the stored inputs.  Infeasibility rolls the
    whole enclosing transaction back -- head, orders and allocations stay at
    their committed state.
    """
    active = _load_active_orders(conn)
    if len(active) > MAX_ACTIVE_ORDERS:
        raise RevisionError('too many active sales orders')
    line_count = sum(len(entry['lines']) for entry in active)
    if line_count > MAX_ACTIVE_LINES:
        raise RevisionError('too many active sales order lines')
    generation = _next_generation(conn)
    try:
        outcome = allocate_commitments(
            periods, items, bom, demand, receipts, order, active)
    except CommitmentInfeasible as error:
        raise ReservationRejected(str(error)) from error
    _persist_allocation(conn, outcome['allocations'], generation, plan_version)
    _bump_generation(conn, generation)
    return outcome, generation


def reserve_order(db_path, payload, crash_hook=None):
    """Reserve one sales order: all lines succeed together or the order is refused.

    Same order id with identical content is an idempotent replay; same id with
    different content, or any id of a previously released order, is rejected.
    Feasibility is decided by one joint allocation over every active order, so
    a new order is never granted at another order's expense.  All writes commit
    in one transaction.
    """
    order_id, lines, canonical, digest = _validate_order_payload(payload)
    conn = connect(db_path)
    try:
        existing = conn.execute(
            'SELECT status, payload_sha256 FROM sales_order WHERE order_id = ?',
            (order_id,)).fetchone() if _has_commitment_tables(conn) else None
        if existing is not None:
            if existing['status'] == 'released':
                raise DuplicateOrder(
                    f'order id {order_id} was released and cannot be reused')
            if existing['payload_sha256'] != digest:
                raise DuplicateOrder(
                    f'order id {order_id} already exists with different content')
            head = conn.execute(
                'SELECT version FROM head WHERE only_id = 1').fetchone()['version']
            return {'order_id': order_id, 'version': head,
                    'generation': int(conn.execute(
                        "SELECT value FROM meta WHERE key = 'allocation_generation'")
                                      .fetchone()['value']),
                    'replayed': True}

        _begin_immediate(conn)
        _ensure_commitment_schema(conn)
        existing = conn.execute(
            'SELECT status, payload_sha256 FROM sales_order WHERE order_id = ?',
            (order_id,)).fetchone()
        if existing is not None:
            if existing['status'] == 'released':
                raise DuplicateOrder(
                    f'order id {order_id} was released and cannot be reused')
            if existing['payload_sha256'] != digest:
                raise DuplicateOrder(
                    f'order id {order_id} already exists with different content')
            conn.commit()
            head = conn.execute(
                'SELECT version FROM head WHERE only_id = 1').fetchone()['version']
            return {'order_id': order_id, 'version': head,
                    'generation': int(conn.execute(
                        "SELECT value FROM meta WHERE key = 'allocation_generation'")
                                      .fetchone()['value']),
                    'replayed': True}

        periods, items, bom, order = _load_static(conn)
        leaf_set = set(leaf_items(order, bom))
        _check_order_static(lines, periods, leaf_set)
        demand, receipts = _load_inputs(conn, items, periods)
        head = conn.execute('SELECT version FROM head WHERE only_id = 1').fetchone()['version']
        active = _load_active_orders(conn)
        if len(active) >= MAX_ACTIVE_ORDERS:
            raise RevisionError('too many active sales orders')
        if sum(len(entry['lines']) for entry in active) + len(lines) > MAX_ACTIVE_LINES:
            raise RevisionError('too many active sales order lines')

        # Insert the new order first, then run the same from-scratch joint
        # re-allocation used by apply/release.  The allocator always starts
        # from the raw FIFO residual, so every active order -- old and new --
        # is accounted for exactly once; an infeasible outcome rolls back the
        # order rows together with the tentative allocation.
        now = _now()
        conn.execute(
            'INSERT INTO sales_order VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (order_id, 'active', digest, canonical, head, None, now, None))
        conn.executemany(
            'INSERT INTO sales_order_line VALUES (?, ?, ?, ?)',
            [(order_id, line['line_no'], line['quantity'], line['due_period'])
             for line in lines])
        candidate_rows = [(order_id, line['line_no'], position, item)
                          for line in lines
                          for position, item in enumerate(line['candidates'])]
        conn.executemany(
            'INSERT INTO sales_order_candidate VALUES (?, ?, ?, ?)', candidate_rows)
        outcome, generation = _recompute_commitments(
            conn, periods, items, bom, order, demand, receipts, head)
        if crash_hook is not None:
            crash_hook(conn)
        conn.commit()
        return {'order_id': order_id, 'version': head,
                'generation': generation, 'replayed': False,
                'allocations': [row for row in outcome['allocations']
                                if row['order_id'] == order_id]}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def release_order(db_path, order_id):
    """Release one active order and re-allocate every remaining active order.

    The order id is retained in released state and can never be reused; the
    freed supply is redistributed jointly, and the replacement allocation
    commits in the same transaction as the status change.
    """
    if type(order_id) is not int or isinstance(order_id, bool) or order_id < 0:
        raise RevisionError('order_id must be a non-negative integer')
    conn = connect(db_path)
    try:
        if not _has_commitment_tables(conn):
            raise RevisionError(f'unknown order {order_id}')
        existing = conn.execute(
            'SELECT status FROM sales_order WHERE order_id = ?',
            (order_id,)).fetchone()
        if existing is None:
            raise RevisionError(f'unknown order {order_id}')
        if existing['status'] == 'released':
            raise RevisionError(f'order {order_id} is already released')
        _begin_immediate(conn)
        _ensure_commitment_schema(conn)
        periods, items, bom, order = _load_static(conn)
        demand, receipts = _load_inputs(conn, items, periods)
        head = conn.execute('SELECT version FROM head WHERE only_id = 1').fetchone()['version']
        conn.execute(
            "UPDATE sales_order SET status = 'released', released_version = ?, "
            'released_at = ? WHERE order_id = ?',
            (head, _now(), order_id))
        conn.execute('DELETE FROM sales_allocation WHERE order_id = ?', (order_id,))
        outcome, generation = _recompute_commitments(
            conn, periods, items, bom, order, demand, receipts, head)
        conn.commit()
        return {'order_id': order_id, 'version': head,
                'generation': generation,
                'allocations': outcome['allocations']}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def availability(db_path):
    """Per-supply-point remaining availability after version FIFO and commitments.

    The database is opened read-only; a pre-v2 database is reported without
    commitment tables (zero committed quantities) rather than being migrated.
    """
    conn = connect(db_path, readonly=True)
    try:
        head_row = conn.execute('SELECT version FROM head WHERE only_id = 1').fetchone()
        if head_row is None:
            raise RevisionError('not an initialized plan database')
        head = head_row['version']
        periods, items, bom, order = _load_static(conn)
        demand, receipts = _load_inputs(conn, items, periods)
        leaves = leaf_items(order, bom)
        points = build_supply_points(periods, items, receipts, leaves)
        gross = leaf_gross_requirements(
            periods, items, bom, demand, receipts, order, set(leaves))
        residual, shortfall = fifo_version_consumption(
            points, periods, set(leaves), gross)
        committed = {point[:3]: 0 for point in points}
        generation = 0
        active_orders = []
        if _has_commitment_tables(conn):
            gen_row = conn.execute(
                "SELECT value FROM meta WHERE key = 'allocation_generation'").fetchone()
            if gen_row is not None:
                generation = int(gen_row['value'])
            active_orders = [row['order_id'] for row in conn.execute(
                "SELECT order_id FROM sales_order WHERE status = 'active' ORDER BY order_id")]
            for row in conn.execute('SELECT * FROM sales_allocation'):
                kind_code = (_ON_HAND_KIND if row['supply_kind'] == 'on_hand'
                             else _RECEIPT_KIND)
                key = (row['item_id'], row['supply_period'], kind_code)
                if key not in committed:
                    raise RevisionError(
                        'corrupt store: allocation against unknown supply point')
                committed[key] += row['quantity']
        supply_rows = []
        for item, period, kind, quantity in points:
            key = (item, period, kind)
            supply_rows.append({
                'item': item,
                'supply_period': period,
                'supply_kind': ('on_hand' if kind == _ON_HAND_KIND
                                else 'scheduled_receipt'),
                'quantity': quantity,
                'version_requirement_consumed': quantity - residual[key],
                'committed': committed[key],
                'remaining': residual[key] - committed[key]})
        return {
            'revision': {'version': head, 'head_version': head},
            'allocation_generation': generation,
            'active_order_ids': active_orders,
            'version_shortfall': [
                {'item': item, 'period': t, 'outstanding': shortfall[item][t]}
                for item in leaves for t in range(periods) if shortfall[item][t]],
            'supply': supply_rows}
    finally:
        conn.close()


def _has_commitment_tables(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sales_order'").fetchone() is not None
