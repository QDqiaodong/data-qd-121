"""Actual-batch lot traceability store and exact-fraction lot recall.

The build side imports an out-of-order event CSV (``type,event,lot,item,
quantity``) into a dedicated SQLite database.  The plan database is opened
read-only: only its item identities and BOM are read.  Rows are streamed into a
staging table in bounded chunks and every validation is a set-based SQL query,
so a 200,000-row input never materialises its rows or dependency edges as
Python collections.  The whole import runs in one transaction; any contract
violation rolls back and deletes the database file, so a rejected batch never
leaves a half-populated store.

Lot production order is derived from lot dependencies with a bulk Kahn
sweep over a bipartite lot/event graph (lot -> consuming event -> produced
lot), which also proves the absence of cycles without loading the graph into
memory.

Recall opens the trace database read-only and propagates one raw-material
receipt lot under the complete-mixing assumption: every consumed lot
contributes proportionally to the consuming event's pooled source quantity,
and every output lot receives that pool proportional to its output share.
All arithmetic uses :class:`fractions.Fraction`; intermediate fractions live
in TEMP tables and are streamed event by event, so splits that re-converge are
counted once and the source quantity attributed to all remaining lots equals
the original receipt exactly.
"""
from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
import tempfile
from fractions import Fraction
from pathlib import Path

from .store import connect as connect_plan

MAX_ROWS = 200_000
MAX_FILE_BYTES = 64 * 1024 * 1024
ROW_QUANTITY_BOUND = 10 ** 9
CHUNK = 1000
EXPECTED_HEADER = ['type', 'event', 'lot', 'item', 'quantity']
ROW_TYPES = ('receipt', 'consume', 'produce')

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE item_ref (
    item_id TEXT PRIMARY KEY,
    is_leaf INTEGER NOT NULL
);
CREATE TABLE event (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('receipt', 'produce')),
    item_id TEXT
);
CREATE TABLE lot (
    lot_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('receipt', 'produce')),
    origin_event TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    seq INTEGER NOT NULL
);
CREATE TABLE event_consume (
    event_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (event_id, lot_id)
);
CREATE TABLE event_produce (
    event_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY (event_id, lot_id)
);
CREATE TABLE produce_order (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE
);
"""


class TraceError(ValueError):
    """A rejected traceability import or recall request."""


def _parse_quantity(token):
    """Strict positive decimal integer; no signs, decimals, unicode digits."""
    if not token or any(not ('0' <= ch <= '9') for ch in token):
        raise TraceError('quantity must be a positive decimal integer')
    value = int(token)
    if not 1 <= value <= ROW_QUANTITY_BOUND:
        raise TraceError('quantity must be between 1 and 10^9')
    return value


def _check_ident(token, line):
    """Inline equivalent of common.ident: nonblank, trimmed, <=128, no controls.

    Kept local so the 200k-row import does not pay three function calls per
    cell; the accepted/rejected string set is identical to the shared helper.
    """
    if (not token or token != token.strip() or len(token) > 128
            or any(ord(ch) < 32 for ch in token)):
        raise TraceError(
            f'line {line}: expected nonblank identifier, <=128 characters')
    return token


def _plan_reference(plan_db):
    """Read item identities and BOM from the plan store, strictly read-only."""
    conn = connect_plan(plan_db, readonly=True)
    try:
        if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='head'").fetchone() is None:
            raise TraceError('not an initialized plan database: ' + str(plan_db))
        _periods, items, bom, _order = _load_plan_static(conn)
    finally:
        conn.close()
    return items, bom


def _load_plan_static(conn):
    from .store import _load_static
    return _load_static(conn)


def build(trace_db, plan_db, events_csv):
    """Import one event CSV into a new trace database, all-or-nothing.

    The database is fully built and validated in a hidden temporary file in
    the target directory and only renamed into place via ``os.replace`` once
    the transaction has committed.  A rejected batch (or a process killed
    mid-import) therefore leaves no trace database at the target path at all,
    let alone a half-populated one.  Raises :class:`TraceError` for every
    contract violation.
    """
    path = Path(trace_db)
    if path.exists() and path.stat().st_size > 0:
        raise TraceError('trace database already exists: ' + str(path))
    items, bom = _plan_reference(plan_db)
    leaf = {item: not edges for item, edges in bom.items()}
    digest = _hash_file(events_csv)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix='.trace-build-', suffix='.db', dir=str(path.parent))
    os.close(handle)
    temp_path = Path(temp_name)
    conn = sqlite3.connect(str(temp_path))
    try:
        # Single-process scratch build into a private temp file: trade
        # durability for speed; the atomic rename publishes only a committed db.
        conn.execute('PRAGMA journal_mode = OFF')
        conn.execute('PRAGMA synchronous = OFF')
        conn.execute('PRAGMA temp_store = MEMORY')
        conn.execute('PRAGMA locking_mode = EXCLUSIVE')
        conn.execute('PRAGMA cache_size = -65536')
        conn.execute('BEGIN')
        for statement in SCHEMA.split(';'):
            if statement.strip():
                conn.execute(statement)
        conn.executemany('INSERT INTO item_ref(item_id, is_leaf) VALUES (?, ?)',
                         [(item, 1 if leaf[item] else 0) for item in sorted(items)])
        conn.execute('CREATE TABLE stage ('
                     'type TEXT NOT NULL, event TEXT NOT NULL, lot TEXT NOT NULL, '
                     'item TEXT NOT NULL, quantity INTEGER NOT NULL)')
        loaded = _stream_staging(conn, events_csv)
        if loaded == 0:
            raise TraceError('event CSV contains no data rows')
        # Staging indexes keep every set-based validation O(n log n); without
        # them the cross joins below degrade to quadratic scans on 200k rows.
        conn.execute('CREATE INDEX idx_stage_event_lot ON stage(event, lot)')
        conn.execute('CREATE INDEX idx_stage_lot ON stage(lot)')
        conn.execute('CREATE INDEX idx_stage_item ON stage(item)')
        conn.execute('CREATE INDEX idx_stage_type ON stage(type)')
        _load_plan_tables(conn, items, leaf, bom)
        _validate(conn)
        _create_persistent_indexes(conn)
        event_count, lot_count, consume_count = _persist(conn)
        conn.executemany('INSERT INTO meta(key, value) VALUES (?, ?)', [
            ('schema_version', '1'),
            ('plan_database', str(Path(plan_db).absolute())),
            ('events_sha256', digest),
            ('row_count', str(loaded)),
            ('event_count', str(event_count)),
            ('lot_count', str(lot_count)),
            ('consume_row_count', str(consume_count))])
        conn.execute('DROP TABLE stage')
        conn.commit()
        conn.close()
        os.replace(temp_path, path)
    except BaseException:
        conn.close()
        temp_path.unlink(missing_ok=True)
        raise
    return {'database': str(path), 'rows': loaded, 'events': event_count,
            'lots': lot_count, 'consumption_rows': consume_count,
            'events_sha256': digest}


def _hash_file(csv_path):
    path = Path(csv_path)
    if not path.is_file():
        raise TraceError('event CSV not found: ' + str(path))
    if path.stat().st_size > MAX_FILE_BYTES:
        raise TraceError('event CSV exceeds 64 MiB')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_staging(conn, csv_path):
    """Parse and insert CSV rows in bounded chunks; nothing is accumulated."""
    buffer = []
    loaded = 0
    with Path(csv_path).open('r', encoding='utf-8', newline='') as stream:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration:
            raise TraceError('event CSV is empty')
        if header != EXPECTED_HEADER:
            raise TraceError('CSV header must be exactly: ' + ','.join(EXPECTED_HEADER))
        for line, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) != 5:
                raise TraceError(f'line {line}: expected 5 columns')
            kind, event, lot, item, token = row
            if kind not in ROW_TYPES:
                raise TraceError(f'line {line}: type must be one of {",".join(ROW_TYPES)}')
            event = _check_ident(event, line)
            lot = _check_ident(lot, line)
            item = _check_ident(item, line)
            try:
                quantity = _parse_quantity(token)
            except TraceError as error:
                raise TraceError(f'line {line}: {error}') from error
            buffer.append((kind, event, lot, item, quantity))
            loaded += 1
            if loaded > MAX_ROWS:
                raise TraceError(f'event CSV exceeds {MAX_ROWS} rows')
            if len(buffer) >= CHUNK:
                conn.executemany('INSERT INTO stage VALUES (?, ?, ?, ?, ?)', buffer)
                buffer.clear()
        if buffer:
            conn.executemany('INSERT INTO stage VALUES (?, ?, ?, ?, ?)', buffer)
    return loaded


def _load_plan_tables(conn, items, leaf, bom):
    conn.execute('CREATE TEMP TABLE plan_item(item_id TEXT PRIMARY KEY, is_leaf INTEGER NOT NULL)')
    conn.executemany('INSERT INTO plan_item VALUES (?, ?)',
                     [(item, 1 if leaf[item] else 0) for item in sorted(items)])
    conn.execute('CREATE TEMP TABLE bom_edge(parent TEXT, component TEXT, quantity INTEGER)')
    conn.executemany('INSERT INTO bom_edge VALUES (?, ?, ?)',
                     [(parent, component, quantity)
                      for parent in sorted(bom)
                      for component, quantity in bom[parent]])


def _scalar_error(conn, sql, message, params=()):
    row = conn.execute(sql, params).fetchone()
    if row is not None:
        raise TraceError(message + ': ' + ', '.join('' if v is None else str(v) for v in row))


def _create_persistent_indexes(conn):
    """Indexes on the committed tables that every recall query relies on."""
    conn.execute('CREATE INDEX idx_consume_lot ON event_consume(lot_id)')
    conn.execute('CREATE INDEX idx_consume_event ON event_consume(event_id)')
    conn.execute('CREATE INDEX idx_produce_event ON event_produce(event_id)')
    conn.execute('CREATE INDEX idx_lot_origin ON lot(origin_event)')


def _validate(conn):
    """All rejection rules as bounded set-based SQL queries."""
    _scalar_error(
        conn,
        'SELECT item FROM stage WHERE item NOT IN (SELECT item_id FROM plan_item) LIMIT 1',
        'unknown item not present in the plan database')
    _scalar_error(
        conn,
        'SELECT lot, COUNT(*) FROM stage WHERE type IN ("receipt", "produce") '
        'GROUP BY lot HAVING COUNT(*) > 1 LIMIT 1',
        'duplicate lot output (lot must be created by exactly one receipt/produce row)')
    _scalar_error(
        conn,
        'SELECT event FROM stage GROUP BY event HAVING '
        'SUM(CASE WHEN type = "receipt" THEN 1 ELSE 0 END) > 0 AND '
        'SUM(CASE WHEN type <> "receipt" THEN 1 ELSE 0 END) > 0 LIMIT 1',
        'receipt event may only contain receipt rows')
    _scalar_error(
        conn,
        'SELECT event FROM stage WHERE type <> "receipt" GROUP BY event HAVING '
        'SUM(CASE WHEN type = "produce" THEN 1 ELSE 0 END) = 0 OR '
        'SUM(CASE WHEN type = "consume" THEN 1 ELSE 0 END) = 0 LIMIT 1',
        'production event requires at least one produce and one consume row')
    _scalar_error(
        conn,
        'SELECT s.lot, s.type, p.item_id FROM stage s JOIN plan_item p ON p.item_id = s.item '
        'WHERE (s.type = "receipt" AND p.is_leaf = 0) OR (s.type = "produce" AND p.is_leaf = 1) '
        'LIMIT 1',
        'receipts create leaf lots only; produce creates non-leaf lots only')
    _scalar_error(
        conn,
        'SELECT event FROM stage WHERE type = "produce" GROUP BY event '
        'HAVING COUNT(DISTINCT item) > 1 LIMIT 1',
        'one production event may only produce one parent item')
    _scalar_error(
        conn,
        'SELECT event, lot, COUNT(*) FROM stage WHERE type = "consume" '
        'GROUP BY event, lot HAVING COUNT(*) > 1 LIMIT 1',
        'duplicate consumption of the same lot within one event')
    conn.execute(
        'CREATE TEMP TABLE lotdef AS SELECT lot, item, event AS origin_event, '
        'type AS kind, SUM(quantity) AS quantity FROM stage '
        'WHERE type IN ("receipt", "produce") GROUP BY lot')
    conn.execute('CREATE UNIQUE INDEX lotdef_lot ON lotdef(lot)')
    conn.execute('CREATE INDEX lotdef_origin ON lotdef(origin_event)')
    _scalar_error(
        conn,
        'SELECT DISTINCT lot FROM stage WHERE type = "consume" AND '
        'lot NOT IN (SELECT lot FROM lotdef) LIMIT 1',
        'dangling consumption reference to an undefined lot')
    _scalar_error(
        conn,
        'SELECT s.lot, s.item, d.item FROM stage s JOIN lotdef d ON d.lot = s.lot '
        'WHERE s.type = "consume" AND s.item <> d.item LIMIT 1',
        'consumption item does not match the lot item')
    # Exact BOM balance per production event: every component's total usage
    # must equal output quantity times the BOM quantity.  This rejects both
    # missing/short components and cumulative over-issue.
    conn.execute(
        'CREATE TEMP TABLE produce_sum AS SELECT event, item, SUM(quantity) AS qty '
        'FROM stage WHERE type = "produce" GROUP BY event, item')
    conn.execute(
        'CREATE TEMP TABLE consume_sum AS SELECT event, item, SUM(quantity) AS qty '
        'FROM stage WHERE type = "consume" GROUP BY event, item')
    conn.execute('CREATE INDEX ps_event ON produce_sum(event)')
    conn.execute('CREATE INDEX cs_event ON consume_sum(event)')
    _scalar_error(
        conn,
        'SELECT p.event, b.component, p.qty * b.quantity AS required, '
        'COALESCE(c.qty, 0) AS used FROM produce_sum p JOIN bom_edge b ON b.parent = p.item '
        'LEFT JOIN consume_sum c ON c.event = p.event AND c.item = b.component '
        'WHERE COALESCE(c.qty, 0) <> p.qty * b.quantity LIMIT 1',
        'component consumption must equal output quantity times BOM quantity')
    _scalar_error(
        conn,
        'SELECT c.event, c.item FROM consume_sum c JOIN produce_sum p ON p.event = c.event '
        'LEFT JOIN bom_edge b ON b.parent = p.item AND b.component = c.item '
        'WHERE b.component IS NULL LIMIT 1',
        'consumed item is not a BOM component of the produced parent')
    _scalar_error(
        conn,
        'SELECT d.lot, d.quantity, u.used FROM lotdef d JOIN ('
        ' SELECT lot, SUM(quantity) AS used FROM stage WHERE type = "consume" GROUP BY lot'
        ') u ON u.lot = d.lot WHERE u.used > d.quantity LIMIT 1',
        'cumulative consumption exceeds the lot quantity')
    _order_lots(conn)


def _order_lots(conn):
    """Bulk Kahn sweep over the bipartite lot/event dependency graph.

    Edges are lot -> event consuming it -> lot produced by it.  The distinct
    ``(event, lot)`` consumption edges live in an indexed TEMP table inside
    SQLite, never as a Python collection.  Each sweep round fires every event
    whose input lots have all been released, then releases the lots those
    events produce.  Readiness is tested set-based with ``NOT EXISTS`` and
    every helper table is indexed, so each round is a small fixed number of
    linear passes; the statement count tracks dependency depth rather than
    row count and no event/lot row is ever returned to Python.
    ``(round, event)`` makes the production sequence deterministic; an
    unfinished remainder proves a dependency cycle.
    """
    conn.execute('CREATE TEMP TABLE edge(event TEXT NOT NULL, lot TEXT NOT NULL)')
    conn.execute(
        'INSERT INTO edge SELECT DISTINCT event, lot FROM stage WHERE type = "consume"')
    conn.execute('CREATE INDEX edge_lot ON edge(lot)')
    conn.execute('CREATE INDEX edge_event ON edge(event)')
    # Lots available as event inputs: receipts are free from the start; a
    # produced lot is released once its single origin event has fired.
    conn.execute('CREATE TEMP TABLE rlot(lot TEXT PRIMARY KEY)')
    conn.execute(
        "INSERT INTO rlot SELECT lot FROM lotdef WHERE kind = 'receipt'")
    conn.execute('CREATE TEMP TABLE fired(event TEXT PRIMARY KEY, round INTEGER NOT NULL)')
    conn.execute('CREATE TEMP TABLE round_ready(event TEXT PRIMARY KEY)')
    total_events = conn.execute(
        'SELECT COUNT(DISTINCT event) FROM edge').fetchone()[0]
    round_no = 0
    while True:
        # Ready = an event not yet fired with no consumed lot still unreleased.
        conn.execute(
            'INSERT INTO round_ready '
            'SELECT DISTINCT e.event FROM edge e WHERE NOT EXISTS ('
            'SELECT 1 FROM edge g LEFT JOIN rlot r ON r.lot = g.lot '
            'WHERE g.event = e.event AND r.lot IS NULL) '
            'AND e.event NOT IN (SELECT event FROM fired)')
        if conn.execute('SELECT COUNT(*) FROM round_ready').fetchone()[0] == 0:
            break
        conn.execute(
            'INSERT INTO fired(event, round) SELECT event, ? FROM round_ready',
            (round_no,))
        # The fired events release every lot they produce (one origin each).
        conn.execute(
            'INSERT OR IGNORE INTO rlot SELECT s.lot FROM stage s '
            "WHERE s.type = 'produce' AND s.event IN (SELECT event FROM round_ready)")
        conn.execute('DELETE FROM round_ready')
        round_no += 1
        if round_no > total_events + 1:
            raise TraceError('cyclic lot dependency detected during topological sweep')
    fired_count = conn.execute('SELECT COUNT(*) FROM fired').fetchone()[0]
    if fired_count != total_events:
        blocked = conn.execute(
            'SELECT event FROM edge WHERE event NOT IN (SELECT event FROM fired) '
            'GROUP BY event ORDER BY event LIMIT 1').fetchone()
        raise TraceError('cyclic lot dependency detected at event: ' + blocked[0])
    conn.execute(
        'INSERT INTO produce_order(seq, event_id) '
        'SELECT ROW_NUMBER() OVER (ORDER BY round, event) - 1, event FROM fired')


def _persist(conn):
    """Move validated staging rows into the committed tables."""
    conn.execute(
        'INSERT INTO event(event_id, kind, item_id) '
        'SELECT event, "receipt", NULL FROM stage WHERE type = "receipt" GROUP BY event')
    conn.execute(
        'INSERT INTO event(event_id, kind, item_id) '
        'SELECT event, "produce", item FROM produce_sum')
    conn.execute(
        'INSERT INTO event_consume SELECT event, lot, item, quantity FROM stage WHERE type = "consume"')
    conn.execute(
        'INSERT INTO event_produce SELECT event, lot, item, quantity FROM stage WHERE type = "produce"')
    # Receipt lots first (sorted by lot id), then produced lots in the event
    # production order and lot id for deterministic lot sequence numbers.
    conn.execute(
        'INSERT INTO lot(lot_id, item_id, kind, origin_event, quantity, seq) '
        'SELECT d.lot, d.item, d.kind, d.origin_event, d.quantity, '
        'ROW_NUMBER() OVER (ORDER BY CASE d.kind WHEN "receipt" THEN 0 ELSE 1 END, '
        'COALESCE(o.seq, -1), d.lot) - 1 FROM lotdef d '
        'LEFT JOIN produce_order o ON o.event_id = d.origin_event')
    counts = (
        conn.execute('SELECT COUNT(*) FROM event').fetchone()[0],
        conn.execute('SELECT COUNT(*) FROM lot').fetchone()[0],
        conn.execute('SELECT COUNT(*) FROM event_consume').fetchone()[0])
    return counts


class RecallResult:
    """Streaming recall of one receipt lot; fractions stay in TEMP tables.

    Use as a context manager.  :meth:`rows` yields one small dict per remaining
    lot that carries a nonzero amount of the source, in lot id order, without
    accumulating the result set.
    """

    def __init__(self, db_path, source_lot):
        self._conn = sqlite3.connect(
            'file:' + str(Path(db_path).absolute()) + '?mode=ro', uri=True)
        self._conn.execute('PRAGMA temp_store = MEMORY')
        conn = self._conn
        try:
            row = conn.execute(
                'SELECT lot_id, item_id, quantity, kind FROM lot WHERE lot_id = ?',
                (source_lot,)).fetchone()
            if row is None:
                raise TraceError('unknown lot: ' + str(source_lot))
            if row[3] != 'receipt':
                raise TraceError(
                    f'recall requires a raw-material receipt lot; {source_lot} is a produced lot')
            self.source = {'lot': row[0], 'item': row[1], 'quantity': row[2]}
            self._propagate()
            self.total = self._check_conservation()
        except BaseException:
            self._conn.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._conn.close()

    def _propagate(self):
        conn = self._conn
        conn.execute('CREATE TEMP TABLE attr(lot TEXT PRIMARY KEY, num INTEGER, den INTEGER)')
        conn.execute(
            'INSERT INTO attr VALUES (?, ?, 1)',
            (self.source['lot'], self.source['quantity']))
        for event, in conn.execute('SELECT event_id FROM produce_order ORDER BY seq'):
            pool = Fraction(0)
            inputs = conn.execute(
                'SELECT c.lot_id, c.quantity, l.quantity, a.num, a.den '
                'FROM event_consume c JOIN lot l ON l.lot_id = c.lot_id '
                'LEFT JOIN attr a ON a.lot = c.lot_id WHERE c.event_id = ?', (event,))
            for lot_id, used, lot_qty, num, den in inputs:
                if num is None:
                    continue
                pool += Fraction(num, den) * Fraction(used, lot_qty)
            total_out = conn.execute(
                'SELECT COALESCE(SUM(quantity), 0) FROM event_produce WHERE event_id = ?',
                (event,)).fetchone()[0]
            if total_out <= 0:
                raise TraceError('internal error: production event without output: ' + event)
            outputs = conn.execute(
                'SELECT lot_id, quantity FROM event_produce WHERE event_id = ?', (event,))
            rows = []
            for lot_id, out_qty in outputs:
                amount = pool * Fraction(out_qty, total_out)
                rows.append((lot_id, amount.numerator, amount.denominator))
            conn.executemany('INSERT INTO attr VALUES (?, ?, ?)', rows)

    def _check_conservation(self):
        """Sum attributed source units over every positive-balance lot."""
        conn = self._conn
        total = Fraction(0)
        rows = conn.execute(
            'SELECT l.lot_id, l.quantity, '
            'l.quantity - COALESCE(SUM(c.quantity), 0) AS remaining, '
            'a.num, a.den FROM lot l LEFT JOIN event_consume c ON c.lot_id = l.lot_id '
            'LEFT JOIN attr a ON a.lot = l.lot_id GROUP BY l.lot_id '
            'HAVING remaining > 0 AND a.num IS NOT NULL ORDER BY l.lot_id')
        for _lot, lot_qty, remaining, num, den in rows:
            total += Fraction(num, den) * Fraction(remaining, lot_qty)
        expected = Fraction(self.source['quantity'], 1)
        if total != expected:
            raise TraceError(
                f'conservation broken: attributed {total} != receipt {expected}')
        return total

    def header(self):
        total = self.total
        return {
            'schema_version': 1,
            'source': self.source,
            'mixing_model': 'complete_mixing_proportional_propagation',
            'total_attributed': _fraction_obj(total),
            'conservation': {
                'expected_quantity': self.source['quantity'],
                'balanced': total == Fraction(self.source['quantity'], 1)}}

    def rows(self):
        """Yield exact-fraction attribution for each remaining lot with source."""
        conn = self._conn
        cursor = conn.execute(
            'SELECT l.lot_id, l.item_id, l.quantity, '
            'l.quantity - COALESCE(SUM(c.quantity), 0) AS remaining, '
            'a.num, a.den FROM lot l LEFT JOIN event_consume c ON c.lot_id = l.lot_id '
            'JOIN attr a ON a.lot = l.lot_id GROUP BY l.lot_id '
            'HAVING remaining > 0 AND a.num <> 0 ORDER BY l.lot_id')
        for lot_id, item_id, lot_qty, remaining, num, den in cursor:
            source_amount = Fraction(num, den) * Fraction(remaining, lot_qty)
            # Source units attributed to one remaining unit of this lot.  It is
            # a concentration, not a bounded proportion: a BOM that consumes
            # several source units per output unit makes it exceed 1.
            source_per_unit = source_amount / remaining
            yield {
                'lot': lot_id,
                'item': item_id,
                'remaining_quantity': remaining,
                'source_quantity': _fraction_obj(source_amount),
                'source_per_unit': _fraction_obj(source_per_unit)}


def _fraction_obj(value):
    return {'numerator': value.numerator, 'denominator': value.denominator,
            'fraction': f'{value.numerator}/{value.denominator}'}


def recall(trace_db, source_lot):
    """Convenience wrapper returning a full report (bounded by result size)."""
    with RecallResult(trace_db, source_lot) as result:
        report = result.header()
        report['remaining_lots'] = list(result.rows())
        return report
