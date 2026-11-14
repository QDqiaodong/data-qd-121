"""Incremental SQLite revision tests.

Every revised version is reconstructed from the stored inputs and fully
re-netted with ``analyze``; the incremental result must match exactly, and
``recomputed_items`` must never include unrelated items.
"""
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from material_plan.model import analyze
from material_plan.store import (BatchConflict, DuplicateBatch, RevisionError,
                                 apply_batch, connect, initialize,
                                 list_versions, load_report,
                                 _load_inputs, _load_static)

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return json.loads((ROOT / 'fixtures' / 'input.json').read_text())


def full_data_from_store(db_path):
    """Rebuild a v1 input document from the current stored cells (the head)."""
    return _document(db_path, None)


def full_data_from_store_at(db_path, version):
    """Rebuild the input document exactly as it existed at ``version``.

    Version 0 inputs come from the fixture; later versions replay the canonical
    batch payloads committed between 1 and ``version``.
    """
    return _document(db_path, version)


def _document(db_path, version):
    conn = connect(db_path)
    try:
        periods, items, bom, order = _load_static(conn)
        demand, receipts = _load_inputs(conn, items, periods)
        if version is not None:
            # Walk backwards: subtract every batch committed after ``version``
            # from the current cells to reconstruct that historical input.
            later = conn.execute(
                'SELECT version, canonical_payload FROM batch WHERE version > ? '
                'ORDER BY version DESC', (version,)).fetchall()
            for row in later:
                payload = json.loads(row['canonical_payload'])
                for kind, target in (('demand', demand), ('receipts', receipts)):
                    for item, period, delta in payload[kind]:
                        target[item][period] -= delta
    finally:
        conn.close()
    return {
        'periods': periods,
        'items': [{'id': item, **items[item]} for item in order],
        'bom': [{'parent': p, 'component': c, 'quantity': q}
                for p in order for c, q in bom[p]],
        'demand': [{'item': i, 'period': t, 'quantity': demand[i][t]}
                   for i in order for t in range(periods) if demand[i][t]],
        'receipts': [{'item': i, 'period': t, 'quantity': receipts[i][t]}
                     for i in order for t in range(periods) if receipts[i][t]]}


class StoreInitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'plan.db'

    def tearDown(self):
        self.temp.cleanup()

    def test_version_zero_matches_full_analysis(self):
        data = fixture()
        initialize(self.path, data, 'a' * 64)
        report = load_report(self.path, 0)
        self.assertEqual(report['result'], analyze(data))
        self.assertEqual(report['revision']['version'], 0)
        self.assertIsNone(report['revision']['batch_id'])
        self.assertEqual(report['recomputed_items'], report['result']['planning_order'])
        self.assertEqual(report['plan_diffs'], {})
        self.assertEqual(load_report(self.path)['revision']['head_version'], 0)

    def test_reinitialize_on_existing_database_rejected(self):
        initialize(self.path, fixture(), 'a' * 64)
        with self.assertRaisesRegex(RevisionError, 'already exists'):
            initialize(self.path, fixture(), 'a' * 64)

    def test_invalid_source_never_creates_database(self):
        bad = copy.deepcopy(fixture())
        bad['items'][0]['lot_size'] = 0
        with self.assertRaises(ValueError):
            initialize(self.path, bad, 'a' * 64)
        self.assertFalse(self.path.exists())

    def test_analysis_failure_inside_transaction_rolls_back_database(self):
        explosive = {
            'periods': 3,
            'items': [{'id': f'I{k}', 'lead_time': 0, 'lot_size': 1,
                       'on_hand': 0, 'safety_stock': 0} for k in range(5)],
            'bom': [{'parent': f'I{k}', 'component': f'I{k + 1}',
                     'quantity': 10 ** 6} for k in range(4)],
            'demand': [{'item': 'I0', 'period': 1, 'quantity': 10 ** 9}],
            'receipts': []}
        with self.assertRaisesRegex(ValueError, '10\\^18'):
            initialize(self.path, explosive, 'a' * 64)
        self.assertFalse(self.path.exists())
        # The path is reusable immediately after the rolled-back attempt.
        initialize(self.path, fixture(), 'a' * 64)
        self.assertEqual(load_report(self.path, 0)['result'], analyze(fixture()))


class DiamondBomTests(unittest.TestCase):
    """P -> {X, Y} -> S with a shared leaf and an unrelated item Z."""

    DATA = {
        'periods': 6,
        'items': [
            {'id': item, 'lead_time': 1, 'lot_size': 10,
             'on_hand': 0, 'safety_stock': 0}
            for item in ('P', 'X', 'Y', 'S', 'Z')],
        'bom': [
            {'parent': 'P', 'component': 'X', 'quantity': 1},
            {'parent': 'P', 'component': 'Y', 'quantity': 1},
            {'parent': 'X', 'component': 'S', 'quantity': 2},
            {'parent': 'Y', 'component': 'S', 'quantity': 3}],
        'demand': [
            {'item': 'P', 'period': 4, 'quantity': 10},
            {'item': 'Z', 'period': 1, 'quantity': 5}],
        'receipts': []}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'diamond.db'
        initialize(self.path, self.DATA, 'b' * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_shared_component_aggregates_both_parent_contributions(self):
        report = load_report(self.path)
        shared = next(p for p in report['result']['plans'] if p['item'] == 'S')
        # X releases 10 (2*10=20) and Y releases 10 (3*10=30) at period 2.
        self.assertEqual(shared['periods'][2]['gross_requirement'], 50)

    def test_delta_withdraws_old_contribution_and_reinjects_new(self):
        version, _ = apply_batch(
            self.path, 'd1', 0,
            {'demand': [{'item': 'P', 'period': 4, 'delta': 10}]})
        report = load_report(self.path, version)
        # Only the diamond subtree is replanned; Z is unrelated.
        self.assertEqual(report['recomputed_items'], ['P', 'X', 'Y', 'S'])
        self.assertNotIn('Z', report['recomputed_items'])
        shared = next(p for p in report['result']['plans'] if p['item'] == 'S')
        self.assertEqual(shared['periods'][2]['gross_requirement'], 100)
        self.assertEqual(report['result'], analyze(full_data_from_store(self.path)))

    def test_every_revision_matches_full_netting_and_keeps_diffs_consistent(self):
        batches = [
            ('d1', 0, {'demand': [{'item': 'P', 'period': 4, 'delta': 10}]}),
            ('d2', 1, {'receipts': [{'item': 'S', 'period': 2, 'delta': 7}]}),
            ('d3', 2, {'demand': [{'item': 'P', 'period': 4, 'delta': -5}],
                       'receipts': [{'item': 'S', 'period': 2, 'delta': -7}]})]
        for batch_id, base, payload in batches:
            version, replayed = apply_batch(self.path, batch_id, base, payload)
            self.assertFalse(replayed)
            report = load_report(self.path, version)
            self.assertEqual(report['result'], analyze(full_data_from_store(self.path)))
            self.assertNotIn('Z', report['recomputed_items'])
            self._assert_diffs_equal_stored_series(version, report)
        self.assertEqual(load_report(self.path)['revision']['version'], 3)

    def _assert_diffs_equal_stored_series(self, version, report):
        previous = load_report(self.path, version - 1)['result']
        old_plans = {p['item']: p for p in previous['plans']}
        for item, diff in report['plan_diffs'].items():
            new = next(p for p in report['result']['plans'] if p['item'] == item)
            old = old_plans[item]
            for entry in diff['series']:
                t = entry['period']
                for field, delta in entry.items():
                    if field == 'period':
                        continue
                    self.assertEqual(
                        delta,
                        new['periods'][t][field] - old['periods'][t][field])
            release_delta = sum(e['delta'] for e in diff['planned_releases'])
            old_total = sum(e['quantity'] for e in old['planned_releases'])
            new_total = sum(e['quantity'] for e in new['planned_releases'])
            self.assertEqual(release_delta, new_total - old_total)


class CoModificationOrderTests(unittest.TestCase):
    """A parent and its child modified in one batch still process topologically."""

    DATA = {
        'periods': 5,
        'items': [
            {'id': 'P', 'lead_time': 1, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0},
            {'id': 'C', 'lead_time': 1, 'lot_size': 1, 'on_hand': 2, 'safety_stock': 0},
            {'id': 'Z', 'lead_time': 1, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0}],
        'bom': [{'parent': 'P', 'component': 'C', 'quantity': 1}],
        'demand': [{'item': 'P', 'period': 2, 'quantity': 3},
                   {'item': 'Z', 'period': 1, 'quantity': 1}],
        'receipts': []}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'order.db'
        initialize(self.path, self.DATA, '9' * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_child_sees_new_parent_contribution_in_same_batch(self):
        version, _ = apply_batch(self.path, 'both', 0, {
            'demand': [{'item': 'P', 'period': 2, 'delta': 4},
                       {'item': 'C', 'period': 0, 'delta': 1}],
            'receipts': [{'item': 'C', 'period': 1, 'delta': 2}]})
        report = load_report(self.path, version)
        self.assertEqual(report['recomputed_items'], ['P', 'C'])
        self.assertNotIn('Z', report['recomputed_items'])
        self.assertEqual(report['result'], analyze(full_data_from_store(self.path)))
        child = next(p for p in report['result']['plans'] if p['item'] == 'C')
        # Independent demand delta at t0, and P's new release contribution at t1.
        self.assertEqual(child['periods'][0]['gross_requirement'], 1)
        self.assertEqual(child['periods'][1]['gross_requirement'], 7)
        self.assertEqual(child['periods'][1]['scheduled_receipt'], 2)


class PropagationStopTests(unittest.TestCase):
    """A buffered parent keeps its releases, so descendants stop propagating."""

    DATA = {
        'periods': 6,
        'items': [
            {'id': 'P', 'lead_time': 1, 'lot_size': 10, 'on_hand': 0, 'safety_stock': 0},
            {'id': 'X', 'lead_time': 1, 'lot_size': 10, 'on_hand': 100, 'safety_stock': 0},
            {'id': 'S', 'lead_time': 1, 'lot_size': 10, 'on_hand': 0, 'safety_stock': 0},
            {'id': 'Z', 'lead_time': 1, 'lot_size': 10, 'on_hand': 0, 'safety_stock': 0}],
        'bom': [
            {'parent': 'P', 'component': 'X', 'quantity': 1},
            {'parent': 'X', 'component': 'S', 'quantity': 1}],
        'demand': [
            {'item': 'P', 'period': 4, 'quantity': 10},
            {'item': 'Z', 'period': 1, 'quantity': 5}],
        'receipts': []}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'stop.db'
        initialize(self.path, self.DATA, 'c' * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_lot_threshold_halts_propagation_at_buffered_parent(self):
        # +5 still nets zero for X (100 on hand covers both 10 and 15 gross),
        # so X's releases stay empty and S must never be replanned.
        version, _ = apply_batch(
            self.path, 'c1', 0,
            {'demand': [{'item': 'P', 'period': 4, 'delta': 5}]})
        report = load_report(self.path, version)
        self.assertEqual(report['recomputed_items'], ['P', 'X'])
        self.assertNotIn('S', report['recomputed_items'])
        self.assertNotIn('Z', report['recomputed_items'])
        self.assertEqual(report['result'], analyze(full_data_from_store(self.path)))
        # The recorded diff must confirm X's releases did not move.
        self.assertEqual(report['plan_diffs']['X']['planned_releases'], [])

    def test_no_full_analysis_when_a_later_change_reaches_the_leaf(self):
        # Drain X's buffer first, then a further P change must reach S but
        # never touch the unrelated item Z.
        apply_batch(self.path, 'drain', 0,
                    {'demand': [{'item': 'P', 'period': 4, 'delta': 95}]})
        version, _ = apply_batch(
            self.path, 'reach', 1,
            {'demand': [{'item': 'P', 'period': 4, 'delta': 10}]})
        report = load_report(self.path, version)
        self.assertEqual(report['recomputed_items'], ['P', 'X', 'S'])
        self.assertNotIn('Z', report['recomputed_items'])
        self.assertEqual(report['result'], analyze(full_data_from_store(self.path)))


class NegativePeriodTests(unittest.TestCase):
    DATA = {
        'periods': 4,
        'items': [
            {'id': 'P', 'lead_time': 3, 'lot_size': 5, 'on_hand': 0, 'safety_stock': 0},
            {'id': 'C', 'lead_time': 1, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0}],
        'bom': [{'parent': 'P', 'component': 'C', 'quantity': 1}],
        'demand': [{'item': 'P', 'period': 1, 'quantity': 3}],
        'receipts': []}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'negative.db'
        initialize(self.path, self.DATA, 'd' * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_past_due_change_stays_isolated_from_in_horizon_gross(self):
        version, _ = apply_batch(
            self.path, 'n1', 0,
            {'demand': [{'item': 'P', 'period': 1, 'delta': 3}]})
        report = load_report(self.path, version)
        self.assertEqual(
            report['result']['past_due_releases'],
            [{'item': 'P', 'release_period': -2, 'receipt_period': 1, 'quantity': 10}])
        outside = report['result']['outside_horizon_component_requirements']
        self.assertEqual([e['quantity'] for e in outside], [10])
        self.assertTrue(all(e['requirement_period'] < 0 for e in outside))
        child = next(p for p in report['result']['plans'] if p['item'] == 'C')
        self.assertTrue(all(row['gross_requirement'] == 0 for row in child['periods']))
        self.assertFalse(report['result']['feasible_within_horizon'])
        self.assertEqual(report['result'], analyze(full_data_from_store(self.path)))

    def test_sub_threshold_negative_change_replans_only_parent(self):
        # +2 keeps the lot at 5: P's releases are unchanged, so C is not enqueued.
        version, _ = apply_batch(
            self.path, 'n2', 0,
            {'demand': [{'item': 'P', 'period': 1, 'delta': 2}]})
        report = load_report(self.path, version)
        self.assertEqual(report['recomputed_items'], ['P'])
        self.assertEqual(report['result'], analyze(full_data_from_store(self.path)))


class BatchRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'rules.db'
        initialize(self.path, fixture(), 'e' * 64)
        self.payload = {'demand': [{'item': 'P', 'period': 3, 'delta': 2}]}

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_batch_identical_content_returns_same_version(self):
        first, replayed = apply_batch(self.path, 'same', 0, self.payload)
        self.assertEqual((first, replayed), (1, False))
        # Replay even with a stale base argument still returns the stored version.
        again, replayed = apply_batch(self.path, 'same', 0, self.payload)
        self.assertEqual((again, replayed), (1, True))
        self.assertEqual(load_report(self.path)['revision']['version'], 1)

    def test_duplicate_batch_different_content_rejected(self):
        apply_batch(self.path, 'same', 0, self.payload)
        with self.assertRaises(DuplicateBatch):
            apply_batch(self.path, 'same', 1,
                        {'demand': [{'item': 'P', 'period': 3, 'delta': 5}]})
        # The rejected attempt created no revision.
        self.assertEqual(load_report(self.path)['revision']['version'], 1)

    def test_new_batch_must_base_on_current_head(self):
        apply_batch(self.path, 'one', 0, self.payload)
        with self.assertRaises(BatchConflict):
            apply_batch(self.path, 'two', 0, self.payload)
        self.assertEqual(load_report(self.path)['revision']['version'], 1)

    def test_same_key_rows_merge_and_negative_result_rejected(self):
        payload = {'demand': [
            {'item': 'P', 'period': 3, 'delta': 1},
            {'item': 'P', 'period': 3, 'delta': 2}]}
        version, _ = apply_batch(self.path, 'merge', 0, payload)
        report = load_report(self.path, version)
        plan_p = next(p for p in report['result']['plans'] if p['item'] == 'P')
        self.assertEqual(plan_p['periods'][3]['gross_requirement'], 7)
        with self.assertRaisesRegex(RevisionError, 'would be -'):
            apply_batch(self.path, 'under', 1,
                        {'demand': [{'item': 'P', 'period': 3, 'delta': -99}]})
        # Rejected batch leaves inputs and head untouched.
        self.assertEqual(load_report(self.path)['revision']['version'], 1)
        self.assertEqual(load_report(self.path, 1)['result'], report['result'])

    def test_rows_netting_to_zero_create_no_revision(self):
        with self.assertRaisesRegex(RevisionError, 'net to zero'):
            apply_batch(self.path, 'noop', 0, {'demand': [
                {'item': 'P', 'period': 3, 'delta': 5},
                {'item': 'P', 'period': 3, 'delta': -5}]})
        self.assertEqual(load_report(self.path)['revision']['version'], 0)

    def test_merged_quantity_upper_bound_enforced(self):
        # A already has a scheduled receipt of 2 at period 2.
        with self.assertRaisesRegex(RevisionError, '10\\^9'):
            apply_batch(self.path, 'cap', 0,
                        {'receipts': [{'item': 'A', 'period': 2, 'delta': 10 ** 9}]})
        version, _ = apply_batch(
            self.path, 'atcap', 0,
            {'receipts': [{'item': 'A', 'period': 2, 'delta': 10 ** 9 - 2}]})
        self.assertEqual(version, 1)
        self.assertEqual(
            load_report(self.path, version)['result'],
            analyze(full_data_from_store(self.path)))

    def test_batch_id_must_be_identifier(self):
        for bad_id in ('', '  ', 'x' * 129):
            with self.assertRaises(RevisionError):
                apply_batch(self.path, bad_id, 0, self.payload)

    def test_payload_validation(self):
        bad_payloads = [
            {'unknown': []},
            {'demand': {}},
            {'demand': [{'item': 'P', 'period': 3}]},
            {'demand': [{'item': 'P', 'period': 3, 'delta': 1.5}]},
            {'demand': [{'item': 'P', 'period': 999, 'delta': 1}]},
            {'demand': [{'item': 'ghost', 'period': 0, 'delta': 1}]},
            {},
        ]
        for payload in bad_payloads:
            with self.assertRaises(RevisionError):
                apply_batch(self.path, 'bad', 0, payload)

    def test_concurrent_batches_only_one_wins(self):
        errors = []

        def worker(batch_id):
            try:
                apply_batch(self.path, batch_id, 0, self.payload)
            except BatchConflict:
                errors.append(batch_id)
            except sqlite3.OperationalError as error:
                errors.append('locked:' + str(error))

        threads = [threading.Thread(target=worker, args=(f't{i}',)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(load_report(self.path)['revision']['version'], 1)
        self.assertEqual(len(errors), 5)
        self.assertTrue(all(isinstance(e, str) for e in errors))
        versions = list_versions(self.path)
        self.assertEqual([row['version'] for row in versions], [0, 1])


class TransactionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'recover.db'
        initialize(self.path, fixture(), 'f' * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_process_termination_before_commit_leaves_no_half_version(self):
        def kill(_conn):
            raise SystemExit('simulated termination')

        with self.assertRaises(SystemExit):
            apply_batch(self.path, 'doomed', 0,
                        {'demand': [{'item': 'P', 'period': 3, 'delta': 1}]},
                        crash_hook=kill)
        # Reopening the store shows the old head; no partial rows survived.
        report = load_report(self.path)
        self.assertEqual(report['revision']['version'], 0)
        conn = connect(self.path)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) c FROM batch').fetchone()['c'], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) c FROM version').fetchone()['c'], 1)
            self.assertEqual(
                conn.execute('SELECT COUNT(DISTINCT version) c FROM plan_snapshot').fetchone()['c'], 1)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) c FROM recomputed_item').fetchone()['c'],
                len(fixture()['items']))
        finally:
            conn.close()
        # The batch id is reusable and then commits cleanly.
        version, _ = apply_batch(
            self.path, 'doomed', 0,
            {'demand': [{'item': 'P', 'period': 3, 'delta': 1}]})
        self.assertEqual(version, 1)
        self.assertEqual(
            load_report(self.path)['result'], analyze(full_data_from_store(self.path)))

    def test_failed_validation_changes_nothing(self):
        before = load_report(self.path, 0)
        with self.assertRaises(RevisionError):
            apply_batch(self.path, 'x', 0,
                        {'receipts': [{'item': 'A', 'period': 2, 'delta': -10 ** 12}]})
        self.assertEqual(load_report(self.path)['revision']['version'], 0)
        self.assertEqual(load_report(self.path, 0), before)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'report.db'
        initialize(self.path, fixture(), 'a' * 64)
        apply_batch(self.path, 'b1', 0,
                    {'demand': [{'item': 'P', 'period': 3, 'delta': 2}]})
        apply_batch(self.path, 'b2', 1,
                    {'receipts': [{'item': 'B', 'period': 0, 'delta': 4}]})

    def tearDown(self):
        self.temp.cleanup()

    def test_any_version_remains_readable_and_self_consistent(self):
        head = load_report(self.path)
        self.assertEqual(head['revision']['version'], 2)
        for version in range(3):
            report = load_report(self.path, version)
            self.assertEqual(report['revision']['version'], version)
            self.assertEqual(report['revision']['head_version'], 2)
            self.assertEqual(report['result'], analyze(full_data_from_store_at(self.path, version)))

    def test_unknown_version_rejected(self):
        with self.assertRaisesRegex(RevisionError, 'unknown version'):
            load_report(self.path, 99)


if __name__ == '__main__':
    unittest.main()
