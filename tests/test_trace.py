"""Actual-batch traceability build and exact-fraction recall tests.

Covers out-of-order multi-level diamonds, partial consumption with exact
fractions, split-and-reconverge flows, every rejection rule with all-or-
nothing rollback, lot-dependency production ordering, read-only plan access,
exact conservation, and bounded Python memory on large inputs.  The SQLite
recall engine is cross-checked against an independent pure-Python Fraction
propagator written directly in this file.
"""
import csv
import json
import os
import sqlite3
import tempfile
import tracemalloc
import unittest
from fractions import Fraction
from pathlib import Path

from material_plan.store import initialize
from material_plan.trace import (MAX_ROWS, RecallResult, TraceError, build,
                                 recall)

ROOT = Path(__file__).resolve().parents[1]
PLAN_SOURCE = ROOT / 'fixtures' / 'input.json'
TRACE_CSV = ROOT / 'fixtures' / 'trace_events.csv'


def plan_data():
    return json.loads(PLAN_SOURCE.read_text())


def write_csv(path, rows):
    with Path(path).open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['type', 'event', 'lot', 'item', 'quantity'])
        writer.writerows(rows)


def reference_recall(db_path, source_lot):
    """Independent complete-mixing propagation in plain Python (Fraction).

    Mirrors the contract: pool = sum of attr(lot) * used/lot_qty over consumed
    lots; each output lot gets pool * out_qty / total_output.  Returns the
    expected source amount attributed to each positive-balance remaining lot.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    lot_qty = {r['lot_id']: r['quantity']
               for r in conn.execute('SELECT lot_id, quantity FROM lot')}
    attr = {source_lot: Fraction(
        conn.execute('SELECT quantity FROM lot WHERE lot_id = ?',
                     (source_lot,)).fetchone()['quantity'], 1)}
    for event, in conn.execute('SELECT event_id FROM produce_order ORDER BY seq'):
        pool = Fraction(0)
        for r in conn.execute(
                'SELECT lot_id, quantity FROM event_consume WHERE event_id = ?', (event,)):
            if r['lot_id'] in attr:
                pool += attr[r['lot_id']] * Fraction(r['quantity'], lot_qty[r['lot_id']])
        outs = conn.execute(
            'SELECT lot_id, quantity FROM event_produce WHERE event_id = ?',
            (event,)).fetchall()
        total_out = sum(r['quantity'] for r in outs)
        for r in outs:
            attr[r['lot_id']] = pool * Fraction(r['quantity'], total_out)
    used = {}
    for r in conn.execute('SELECT lot_id, SUM(quantity) AS q FROM event_consume GROUP BY lot_id'):
        used[r['lot_id']] = r['q']
    expected = {}
    for lot, qty in lot_qty.items():
        remaining = qty - used.get(lot, 0)
        amount = attr.get(lot, Fraction(0)) * Fraction(remaining, qty)
        if remaining > 0 and amount != 0:
            expected[lot] = amount
    conn.close()
    return expected


class BuildFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.plan_db = self.dir / 'plan.db'
        self.trace_db = self.dir / 'trace.db'
        initialize(self.plan_db, plan_data(), '0' * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_builds_fixture_and_records_order_and_metadata(self):
        summary = build(self.trace_db, self.plan_db, TRACE_CSV)
        self.assertEqual(summary['rows'], 10)
        self.assertEqual(summary['lots'], 5)
        self.assertEqual(summary['events'], 4)
        conn = sqlite3.connect(str(self.trace_db))
        # Receipt lots precede produced lots; production follows lot dependencies.
        order = [r[0] for r in conn.execute(
            'SELECT event_id FROM produce_order ORDER BY seq')]
        self.assertEqual(order, ['E1', 'E2'])
        kinds = conn.execute('SELECT kind, COUNT(*) FROM lot GROUP BY kind').fetchall()
        self.assertEqual(dict(kinds), {'receipt': 3, 'produce': 2})
        meta = dict(conn.execute('SELECT key, value FROM meta').fetchall())
        self.assertEqual(meta['row_count'], '10')
        self.assertEqual(meta['lot_count'], '5')
        self.assertEqual(len(meta['events_sha256']), 64)
        conn.close()

    def test_out_of_order_rows_resolve_to_same_store(self):
        with TRACE_CSV.open() as handle:
            rows = list(csv.reader(handle))
        header, data_rows = rows[0], rows[1:]
        shuffled = self.dir / 'shuffled.csv'
        with shuffled.open('w', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(reversed(data_rows))
        build(self.trace_db, self.plan_db, TRACE_CSV)
        other = self.dir / 'shuffled.db'
        build(other, self.plan_db, shuffled)
        tables = ('event', 'lot', 'event_consume', 'event_produce', 'produce_order')
        snapshots = {}
        for db in (self.trace_db, other):
            conn = sqlite3.connect(str(db))
            snapshots[db] = {t: conn.execute(
                f'SELECT * FROM {t} ORDER BY 1,2').fetchall() for t in tables}
            conn.close()
        self.assertEqual(snapshots[self.trace_db], snapshots[other])

    def test_plan_database_is_only_read_not_modified(self):
        before = self.plan_db.read_bytes()
        build(self.trace_db, self.plan_db, TRACE_CSV)
        self.assertEqual(self.plan_db.read_bytes(), before)
        # A read-only URI plan database is sufficient for build.
        ro = self.dir / 'ro-plan.db'
        ro.write_bytes(before)
        os.chmod(ro, 0o444)
        build(self.dir / 'from-ro.db', ro, TRACE_CSV)

    def test_recall_matches_independent_reference_for_every_receipt_lot(self):
        build(self.trace_db, self.plan_db, TRACE_CSV)
        receipt_lots = ['a1', 'a2', 'b1']
        for source in receipt_lots:
            with self.subTest(source=source):
                report = recall(self.trace_db, source)
                expected = reference_recall(self.trace_db, source)
                got = {r['lot']: Fraction(r['source_quantity']['numerator'],
                                          r['source_quantity']['denominator'])
                       for r in report['remaining_lots']}
                self.assertEqual(got, expected)
                total = sum(got.values(), Fraction(0))
                qty_conn = sqlite3.connect(str(self.trace_db))
                quantities = dict(qty_conn.execute(
                    'SELECT lot_id, quantity FROM lot').fetchall())
                qty_conn.close()
                self.assertEqual(total, Fraction(quantities[source], 1))
                self.assertTrue(report['conservation']['balanced'])

    def test_hand_checked_fraction_amounts(self):
        build(self.trace_db, self.plan_db, TRACE_CSV)
        report = recall(self.trace_db, 'a1')
        by_lot = {r['lot']: r for r in report['remaining_lots']}
        # a1 = 10: E1 uses 5/10 -> P1 carries 5; E2 uses 4/10 of the remaining
        # pool -> P2 carries 4; one raw unit stays in the a1 lot.
        self.assertEqual(by_lot['P1']['source_quantity']['fraction'], '5/1')
        self.assertEqual(by_lot['P2']['source_quantity']['fraction'], '4/1')
        self.assertEqual(by_lot['a1']['source_quantity']['fraction'], '1/1')
        self.assertEqual(by_lot['P1']['source_per_unit']['fraction'], '5/4')
        self.assertEqual(by_lot['a1']['remaining_quantity'], 1)
        self.assertEqual(report['total_attributed']['fraction'], '10/1')


class PartialConsumptionDiamondTests(unittest.TestCase):
    """Multi-level diamond with mixed lots, splits and reconvergence."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.plan_db = self.dir / 'plan.db'
        data = {
            'periods': 3,
            'items': [{'id': n, 'lead_time': 0, 'lot_size': 1,
                       'on_hand': 0, 'safety_stock': 0}
                      for n in ('P', 'Q', 'X')],
            'bom': [{'parent': 'Q', 'component': 'X', 'quantity': 1},
                    {'parent': 'P', 'component': 'Q', 'quantity': 1}],
            'demand': [], 'receipts': []}
        initialize(self.plan_db, data, '0' * 64)
        # q1 = 10 from x1(3)+x2(7); q2 = 4 wholly from x1; p1 = 5 from
        # q1(3) + q2(2).  Hand-derived x1 shares are asserted below.
        self.rows = [
            ('produce', 'E1', 'q1', 'Q', 10), ('consume', 'E1', 'x1', 'X', 3),
            ('consume', 'E1', 'x2', 'X', 7),
            ('produce', 'E2', 'q2', 'Q', 4), ('consume', 'E2', 'x1', 'X', 4),
            ('produce', 'E3', 'p1', 'P', 5), ('consume', 'E3', 'q1', 'Q', 3),
            ('consume', 'E3', 'q2', 'Q', 2),
            ('receipt', 'R1', 'x1', 'X', 12),
            ('receipt', 'R1', 'x2', 'X', 7)]
        self.csv = self.dir / 'events.csv'
        write_csv(self.csv, self.rows)
        self.trace_db = self.dir / 'trace.db'
        build(self.trace_db, self.plan_db, self.csv)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_fractions_and_reconvergence(self):
        report = recall(self.trace_db, 'x1')
        by_lot = {r['lot']: r for r in report['remaining_lots']}
        self.assertEqual(by_lot['p1']['source_quantity']['fraction'], '29/10')
        self.assertEqual(by_lot['q1']['source_quantity']['fraction'], '21/10')
        self.assertEqual(by_lot['q2']['source_quantity']['fraction'], '2/1')
        self.assertEqual(by_lot['x1']['source_quantity']['fraction'], '5/1')
        self.assertEqual(by_lot['p1']['source_per_unit']['fraction'], '29/50')
        self.assertEqual(report['total_attributed']['fraction'], '12/1')
        # The second source only flows through q1 and partially into p1.
        report2 = recall(self.trace_db, 'x2')
        by_lot2 = {r['lot']: r for r in report2['remaining_lots']}
        self.assertEqual(by_lot2['p1']['source_quantity']['fraction'], '21/10')
        self.assertEqual(by_lot2['q1']['source_quantity']['fraction'], '49/10')
        self.assertEqual(set(by_lot2), {'p1', 'q1'})
        self.assertEqual(report2['total_attributed']['fraction'], '7/1')

    def test_engine_equals_reference(self):
        for source in ('x1', 'x2'):
            expected = reference_recall(self.trace_db, source)
            report = recall(self.trace_db, source)
            got = {r['lot']: Fraction(r['source_quantity']['numerator'],
                                      r['source_quantity']['denominator'])
                   for r in report['remaining_lots']}
            self.assertEqual(got, expected)


class MultiOutputEventTests(unittest.TestCase):
    """One production event producing several new parent lots at once."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.plan_db = self.dir / 'plan.db'
        data = {'periods': 2,
                'items': [{'id': n, 'lead_time': 0, 'lot_size': 1,
                           'on_hand': 0, 'safety_stock': 0} for n in ('P', 'A')],
                'bom': [{'parent': 'P', 'component': 'A', 'quantity': 1}],
                'demand': [], 'receipts': []}
        initialize(self.plan_db, data, '0' * 64)
        self.rows = [
            ('receipt', 'R1', 'a', 'A', 4),
            ('produce', 'E1', 'P1', 'P', 3), ('produce', 'E1', 'P2', 'P', 1),
            ('consume', 'E1', 'a', 'A', 4)]
        self.csv_path = self.dir / 'events.csv'
        write_csv(self.csv_path, self.rows)
        self.trace_db = self.dir / 'trace.db'
        build(self.trace_db, self.plan_db, self.csv_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_output_lots_share_the_pool_proportionally(self):
        report = recall(self.trace_db, 'a')
        amounts = {r['lot']: r['source_quantity']['fraction']
                   for r in report['remaining_lots']}
        self.assertEqual(amounts, {'P1': '3/1', 'P2': '1/1'})
        for r in report['remaining_lots']:
            self.assertEqual(r['source_per_unit']['fraction'], '1/1')
        self.assertEqual(report['total_attributed']['fraction'], '4/1')
        self.assertEqual(report, recall(self.trace_db, 'a'))


class RejectionRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.plan_db = self.dir / 'plan.db'
        initialize(self.plan_db, plan_data(), '0' * 64)
        self.good = [
            ('produce', 'E1', 'P1', 'P', 4),
            ('consume', 'E1', 'a2', 'A', 8), ('consume', 'E1', 'b1', 'B', 4),
            ('receipt', 'R1', 'a2', 'A', 8), ('receipt', 'R2', 'b1', 'B', 5)]

    def tearDown(self):
        self.temp.cleanup()

    def _reject(self, name, rows, needle):
        csv_path = self.dir / f'{name}.csv'
        write_csv(csv_path, rows)
        db = self.dir / f'{name}.db'
        with self.assertRaises(TraceError) as ctx:
            build(db, self.plan_db, csv_path)
        self.assertIn(needle, str(ctx.exception))
        self.assertFalse(db.exists(), 'rejected batch left a database file')
        self.assertEqual(list(self.dir.glob('.trace-build-*')), [])

    def test_unknown_item(self):
        self._reject('unknown', self.good + [('receipt', 'R9', 'z9', 'Z', 1)],
                     'unknown item')

    def test_duplicate_output(self):
        self._reject('dup', self.good + [
            ('produce', 'E9', 'P1', 'P', 1), ('consume', 'E9', 'a2', 'A', 2),
            ('consume', 'E9', 'b1', 'B', 1)], 'duplicate lot output')

    def test_dangling_reference(self):
        self._reject('dangling', [r for r in self.good if r[2] != 'b1'] + [
            ('produce', 'E2', 'P2', 'P', 1), ('consume', 'E2', 'b9', 'B', 1),
            ('consume', 'E2', 'a2', 'A', 2)], 'dangling')

    def test_cumulative_overissue(self):
        self._reject('over', [
            ('produce', 'E1', 'P1', 'P', 4), ('consume', 'E1', 'b1', 'B', 4),
            ('consume', 'E1', 'a2', 'A', 8),
            ('produce', 'E2', 'P2', 'P', 2), ('consume', 'E2', 'b1', 'B', 2),
            ('consume', 'E2', 'a2', 'A', 4),
            ('receipt', 'R1', 'a2', 'A', 12), ('receipt', 'R2', 'b1', 'B', 5)],
            'exceeds the lot quantity')

    def test_bom_imbalance(self):
        self._reject('imbalance', [
            ('produce', 'E1', 'P1', 'P', 4), ('consume', 'E1', 'a2', 'A', 6),
            ('consume', 'E1', 'b1', 'B', 4),
            ('receipt', 'R1', 'a2', 'A', 8), ('receipt', 'R2', 'b1', 'B', 5)],
            'BOM quantity')

    def test_receipt_on_non_leaf_and_produce_on_leaf(self):
        self._reject('receipt_parent', self.good + [('receipt', 'R9', 'p9', 'P', 1)],
                     'receipts create leaf lots')
        self._reject('produce_leaf', self.good + [
            ('produce', 'E9', 'a9', 'A', 1), ('consume', 'E9', 'b1', 'B', 1)],
            'produce creates non-leaf')

    def test_mixed_receipt_event_and_incomplete_production_event(self):
        self._reject('mixed', self.good + [('consume', 'R1', 'a2', 'A', 1)],
                     'receipt event may only')
        self._reject('incomplete', self.good + [('consume', 'E9', 'a2', 'A', 1)],
                     'production event requires')

    def test_item_mismatch_and_duplicate_consume(self):
        self._reject('mismatch', [
            ('produce', 'E1', 'P1', 'P', 4), ('consume', 'E1', 'b1', 'A', 8),
            ('consume', 'E1', 'a2', 'B', 4),
            ('receipt', 'R1', 'a2', 'A', 8), ('receipt', 'R2', 'b1', 'B', 5)],
            'does not match the lot item')
        self._reject('dupconsume', [
            ('produce', 'E1', 'P1', 'P', 4), ('consume', 'E1', 'a2', 'A', 4),
            ('consume', 'E1', 'a2', 'A', 4), ('consume', 'E1', 'b1', 'B', 4),
            ('receipt', 'R1', 'a2', 'A', 8), ('receipt', 'R2', 'b1', 'B', 5)],
            'duplicate consumption')

    def test_malformed_header_and_quantity(self):
        bad_header = self.dir / 'h.csv'
        bad_header.write_text('a,b,c,d,e\nreceipt,R1,x1,B,1\n')
        with self.assertRaises(TraceError):
            build(self.dir / 'h.db', self.plan_db, bad_header)
        self.assertFalse((self.dir / 'h.db').exists())
        write_csv(self.dir / 'zero.csv', [('receipt', 'R1', 'x1', 'B', 0)])
        with self.assertRaises(TraceError):
            build(self.dir / 'zero.db', self.plan_db, self.dir / 'zero.csv')

    def test_existing_database_is_not_overwritten(self):
        write_csv(self.dir / 'g.csv', self.good)
        target = self.dir / 'g.db'
        build(target, self.plan_db, self.dir / 'g.csv')
        with self.assertRaises(TraceError):
            build(target, self.plan_db, self.dir / 'g.csv')

    def test_recall_rejects_non_receipt_and_unknown_lot(self):
        write_csv(self.dir / 'g.csv', self.good)
        db = self.dir / 'g.db'
        build(db, self.plan_db, self.dir / 'g.csv')
        with self.assertRaises(TraceError):
            recall(db, 'P1')
        with self.assertRaises(TraceError):
            recall(db, 'nope')

    def test_cycle_detection_unit(self):
        # A lot-level cycle cannot satisfy an acyclic BOM end to end, so the
        # Kahn guard is exercised directly against a staged cyclic graph.
        from material_plan.trace import _order_lots
        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE stage(type TEXT, event TEXT, lot TEXT, '
                     'item TEXT, quantity INTEGER)')
        conn.executemany('INSERT INTO stage VALUES (?,?,?,?,?)', [
            ('produce', 'E1', 'q1', 'Q', 1), ('consume', 'E1', 'p1', 'P', 1),
            ('produce', 'E2', 'p1', 'P', 1), ('consume', 'E2', 'q1', 'Q', 1)])
        conn.execute('CREATE TEMP TABLE lotdef AS SELECT lot, item, event AS '
                     'origin_event, type AS kind, SUM(quantity) AS quantity '
                     'FROM stage WHERE type IN ("receipt","produce") GROUP BY lot')
        conn.execute('CREATE TEMP TABLE produce_order(seq INTEGER PRIMARY KEY, '
                     'event_id TEXT NOT NULL UNIQUE)')
        with self.assertRaises(TraceError):
            _order_lots(conn)


class LargeBoundedMemoryTests(unittest.TestCase):
    """Wide multi-level chain: forces many Kahn rounds and chunked streaming."""

    def _generate(self, width, depth, path):
        names = ['X'] + [f'C{i}' for i in range(depth)]
        rows = [('receipt', f'R{k}', f'x-{k}', 'X', 4) for k in range(width)]
        for level in range(depth):
            parent, comp = names[level + 1], names[level]
            for k in range(width):
                event = f'E{level}-{k}'
                rows.append(('produce', event,
                             f'{parent.lower()}-{k}', parent, 4))
                rows.append(('consume', event,
                             f'{comp.lower()}-{k}' if comp != 'X' else f'x-{k}',
                             comp, 4))
        write_csv(path, rows)
        return names, rows

    def _plan(self, names, path):
        data = {'periods': 8,
                'items': [{'id': n, 'lead_time': 0, 'lot_size': 1,
                           'on_hand': 0, 'safety_stock': 0} for n in names],
                'bom': [{'parent': names[i + 1], 'component': names[i],
                         'quantity': 1} for i in range(len(names) - 1)],
                'demand': [], 'receipts': []}
        initialize(path, data, '0' * 64)

    def test_wide_chain_bounded_memory_and_conservation(self):
        width, depth = 2000, 6          # 26000 rows, many parallel sweep rounds
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            plan_db, trace_db = td / 'plan.db', td / 'trace.db'
            csv_path = td / 'events.csv'
            names, _ = self._generate(width, depth, csv_path)
            self._plan(names, plan_db)
            tracemalloc.start()
            build(trace_db, plan_db, csv_path)
            _, build_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertLess(build_peak, 16 * 1024 * 1024, build_peak)
            tracemalloc.start()
            with RecallResult(trace_db, 'x-0') as result:
                header = result.header()
                rows = list(result.rows())
            _, recall_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertLess(recall_peak, 16 * 1024 * 1024, recall_peak)
            self.assertTrue(header['conservation']['balanced'])
            self.assertEqual(header['total_attributed']['fraction'], '4/1')
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['lot'], f'c{depth - 1}-0')
            self.assertEqual(rows[0]['source_quantity']['fraction'], '4/1')

    @unittest.skipUnless(os.environ.get('RUN_FULL_200K'),
                         'set RUN_FULL_200K=1 for the 200k-row stress test')
    def test_full_200k_rows(self):
        width, depth = 8000, 12         # exactly 200000 CSV rows
        self.assertEqual(width + depth * width * 2, MAX_ROWS)
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            plan_db, trace_db = td / 'plan.db', td / 'trace.db'
            csv_path = td / 'events.csv'
            names, _ = self._generate(width, depth, csv_path)
            self._plan(names, plan_db)
            tracemalloc.start()
            summary = build(trace_db, plan_db, csv_path)
            _, build_peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertEqual(summary['rows'], MAX_ROWS)
            self.assertLess(build_peak, 64 * 1024 * 1024, build_peak)
            report = recall(trace_db, 'x-0')
            self.assertTrue(report['conservation']['balanced'])
            self.assertEqual(report['total_attributed']['fraction'], '4/1')


if __name__ == '__main__':
    unittest.main()
