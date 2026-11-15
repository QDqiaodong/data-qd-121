"""Leaf-item sales commitment tests: reserve / release / availability.

The allocator is exercised both directly (``model.allocate_commitments``) for
the deterministic lexicographic rule and through the SQLite store for the
transactional, idempotency and migration contracts.  The headline business
scenarios come straight from the specification:

* a ``{A, B}`` order takes A first; a later ``{A}`` order still succeeds;
* replaying a released order id never revives it;
* withdrawing an arrival forces a re-allocation onto B; withdrawing B as well
  must be rejected with the head and allocations untouched;
* concurrent reservations and pre-commit termination never leave a half order.
"""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from material_plan.model import (CommitmentInfeasible, allocate_commitments,
                                 build_supply_points,
                                 fifo_version_consumption,
                                 leaf_gross_requirements, leaf_items)
from material_plan.store import (BatchConflict, DuplicateOrder,
                                 ReservationRejected, RevisionError,
                                 SCHEMA_VERSION, _load_inputs, _load_static,
                                 apply_batch, availability, connect,
                                 initialize, load_report, release_order,
                                 reserve_order)

ROOT = Path(__file__).resolve().parents[1]

# Two leaf items A and B (plus one parent P whose releases add leaf gross
# requirements in the FIFO tests).  A: 1 on hand + receipts at t1/t2; B: one
# receipt at t2.
DATA = {
    'periods': 6,
    'items': [
        {'id': 'P', 'lead_time': 1, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0},
        {'id': 'A', 'lead_time': 1, 'lot_size': 1, 'on_hand': 1, 'safety_stock': 0},
        {'id': 'B', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0}],
    'bom': [{'parent': 'P', 'component': 'A', 'quantity': 1}],
    'demand': [],
    'receipts': [
        {'item': 'A', 'period': 1, 'quantity': 1},
        {'item': 'A', 'period': 2, 'quantity': 1},
        {'item': 'B', 'period': 2, 'quantity': 5}]}

ORDER_AB = {'order_id': 1, 'lines': [
    {'line_no': 0, 'candidates': ['A', 'B'], 'quantity': 2, 'due_period': 3}]}
ORDER_A = {'order_id': 2, 'lines': [
    {'line_no': 0, 'candidates': ['A'], 'quantity': 1, 'due_period': 3}]}


def order_payload(order_id, lines):
    return {'order_id': order_id, 'lines': lines}


class AllocatorModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'plan.db'
        initialize(self.db, DATA, 'a' * 64)
        conn = connect(self.db)
        self.periods, self.items, self.bom, self.order = _load_static(conn)
        self.demand, self.receipts = _load_inputs(conn, self.items, self.periods)
        conn.close()
        self.leaves = set(leaf_items(self.order, self.bom))

    def tearDown(self):
        self.temp.cleanup()

    def _allocate(self, orders):
        return allocate_commitments(
            self.periods, self.items, self.bom, self.demand, self.receipts,
            self.order, orders)

    def test_candidate_ab_takes_a_first_and_later_a_only_order_still_succeeds(self):
        first = self._allocate([ORDER_AB])
        # A supply (1 on hand + receipt 1 at t1) is lexicographically earlier
        # than B, so the {A,B} line fills from A first.
        self.assertEqual(
            [(r['item'], r['supply_period'], r['quantity'])
             for r in first['allocations']],
            [('A', 0, 1), ('A', 1, 1)])
        both = self._allocate([ORDER_AB, ORDER_A])
        by_order = {}
        for row in both['allocations']:
            by_order.setdefault(row['order_id'], []).append(
                (row['item'], row['supply_period'], row['quantity']))
        self.assertEqual(by_order[1], [('A', 0, 1), ('A', 1, 1)])
        # The A-only order takes the last A unit (the t2 receipt); B stays free.
        self.assertEqual(by_order[2], [('A', 2, 1)])

    def test_joint_allocation_never_starves_a_later_order(self):
        # Two A-only orders competing for three A units both succeed; a fourth
        # unit cannot be granted even though B has stock (candidate restriction).
        outcome = self._allocate([
            order_payload(1, [{'line_no': 0, 'candidates': ['A'],
                               'quantity': 2, 'due_period': 3}]),
            order_payload(2, [{'line_no': 0, 'candidates': ['A'],
                               'quantity': 1, 'due_period': 3}])])
        self.assertEqual(sum(r['quantity'] for r in outcome['allocations']), 3)
        with self.assertRaises(CommitmentInfeasible):
            self._allocate([
                order_payload(1, [{'line_no': 0, 'candidates': ['A'],
                                   'quantity': 2, 'due_period': 3}]),
                order_payload(2, [{'line_no': 0, 'candidates': ['A'],
                                   'quantity': 2, 'due_period': 3}])])

    def test_due_date_excludes_later_supply(self):
        # Due at period 1: only on-hand (t0) and the t1 receipt are usable.
        early = order_payload(7, [{'line_no': 0, 'candidates': ['A'],
                                   'quantity': 3, 'due_period': 1}])
        with self.assertRaises(CommitmentInfeasible):
            self._allocate([early])
        feasible = order_payload(7, [{'line_no': 0, 'candidates': ['A'],
                                      'quantity': 2, 'due_period': 1}])
        outcome = self._allocate([feasible])
        self.assertEqual(
            sorted((r['supply_period'], r['quantity']) for r in outcome['allocations']),
            [(0, 1), (1, 1)])

    def test_lexicographically_greatest_vector_under_fixed_variable_order(self):
        # One line that can be served from either A or B.  The greatest vector
        # puts the maximum possible quantity on the earliest variable (A t0),
        # then A t1, A t2, then B t2.
        outcome = self._allocate([order_payload(9, [
            {'line_no': 0, 'candidates': ['A', 'B'], 'quantity': 4,
             'due_period': 3}])])
        self.assertEqual(
            [(r['item'], r['supply_period'], r['quantity'])
             for r in outcome['allocations']],
            [('A', 0, 1), ('A', 1, 1), ('A', 2, 1), ('B', 2, 1)])
        # Earlier order id wins the lexicographically earlier supply when both
        # orders have the same candidate set.
        outcome = self._allocate([
            order_payload(1, [{'line_no': 0, 'candidates': ['A', 'B'],
                               'quantity': 2, 'due_period': 3}]),
            order_payload(2, [{'line_no': 0, 'candidates': ['A', 'B'],
                               'quantity': 2, 'due_period': 3}])])
        first = [r for r in outcome['allocations'] if r['order_id'] == 1]
        self.assertEqual(
            [(r['item'], r['supply_period']) for r in first],
            [('A', 0), ('A', 1)])

    def test_multi_line_order_splits_integer_units(self):
        payload = order_payload(3, [
            {'line_no': 0, 'candidates': ['A'], 'quantity': 2, 'due_period': 3},
            {'line_no': 1, 'candidates': ['B'], 'quantity': 3, 'due_period': 3}])
        outcome = self._allocate([payload])
        totals = {}
        for row in outcome['allocations']:
            key = (row['order_id'], row['line_no'])
            totals[key] = totals.get(key, 0) + row['quantity']
        self.assertEqual(totals, {(3, 0): 2, (3, 1): 3})
        self.assertTrue(all(isinstance(r['quantity'], int)
                            for r in outcome['allocations']))

    def test_version_gross_fifo_has_priority_and_carries_shortfall(self):
        # Add independent demand for A at t0: FIFO consumes the on-hand unit
        # before any commitment can see it.
        self.demand['A'][0] = 1
        outcome = self._allocate([order_payload(4, [
            {'line_no': 0, 'candidates': ['A', 'B'], 'quantity': 3,
             'due_period': 3}])])
        items = [(r['item'], r['supply_period'], r['quantity'])
                 for r in outcome['allocations']]
        self.assertNotIn(('A', 0, 1), items)
        self.assertEqual(items, [('A', 1, 1), ('A', 2, 1), ('B', 2, 1)])
        # Demand beyond all supply carries as outstanding shortfall and the
        # residual offered to commitments shrinks accordingly.
        points = build_supply_points(self.periods, self.items, self.receipts,
                                     self.leaves)
        self.demand['A'][3] = 8
        gross = leaf_gross_requirements(
            self.periods, self.items, self.bom, self.demand, self.receipts,
            self.order, self.leaves)
        residual, shortfall = fifo_version_consumption(
            points, self.periods, self.leaves, gross)
        # t0 demand consumes the on-hand unit; the carried t3 requirement of 8
        # then drains the two receipts (2 units) and leaves 6 outstanding.
        self.assertEqual(shortfall['A'][3], 6)
        # Every A supply point is exhausted; untouched B supply remains.
        self.assertTrue(all(value == 0 for key, value in residual.items()
                            if key[0] == 'A'))
        self.assertEqual(residual[('B', 2, 1)], 5)

    def test_parent_release_feeds_leaf_gross_before_commitments(self):
        # P demand at period 3 releases at t2 (lead time 1), creating an A
        # gross requirement of one unit at t2.  FIFO serves version gross
        # before any commitment, starting from the earliest supply point, so
        # the on-hand unit (t0) is consumed first and the order's two units
        # can only come from the t1 and t2 receipts.
        self.demand['P'][3] = 1
        outcome = self._allocate([ORDER_AB])
        self.assertEqual(
            [(r['item'], r['supply_period']) for r in outcome['allocations']],
            [('A', 1), ('A', 2)])
        # The version shortfall is zero everywhere: the gross requirement was
        # fully covered before commitments were considered.
        self.assertTrue(
            all(value == 0 for values in outcome['shortfall'].values()
                for value in values))
        # And the on-hand point shows one unit consumed by the version step.
        on_hand = next(row for row in outcome['points'] if row[2] == 0)
        self.assertEqual(outcome['residual_after_version'][on_hand[:3]], 0)


class ReserveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'commit.db'
        initialize(self.db, DATA, 'b' * 64)

    def tearDown(self):
        self.temp.cleanup()

    def test_reserve_all_or_nothing_and_idempotent_replay(self):
        result = reserve_order(self.db, ORDER_AB)
        self.assertFalse(result['replayed'])
        self.assertEqual(result['version'], 0)
        self.assertEqual(
            [(r['item'], r['supply_period'], r['quantity'])
             for r in result['allocations']],
            [('A', 0, 1), ('A', 1, 1)])
        # Identical replay returns the same state and never changes generation.
        replay = reserve_order(self.db, ORDER_AB)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['generation'], result['generation'])
        # Same id with different content is a conflict.
        changed = json.loads(json.dumps(ORDER_AB))
        changed['lines'][0]['quantity'] = 3
        with self.assertRaises(DuplicateOrder):
            reserve_order(self.db, changed)
        # The rejected replay left the stored allocation untouched.
        av = availability(self.db)
        committed = sum(r['committed'] for r in av['supply'])
        self.assertEqual(committed, 2)

    def test_joint_allocation_refuses_only_genuinely_infeasible_orders(self):
        reserve_order(self.db, ORDER_AB)
        result = reserve_order(self.db, ORDER_A)
        self.assertFalse(result['replayed'])
        # The joint re-allocation gives order 2 the lexicographically latest A
        # point that keeps both promises; order 1 keeps the two earlier ones.
        self.assertEqual(
            sorted((r['item'], r['supply_period'], r['quantity'])
                   for r in result['allocations']),
            [('A', 2, 1)])

        # Three additional A-only units cannot fit: even after order 1 ({A,B})
        # surrenders both A units and moves entirely to B, A-only demand is
        # four units (order 2 plus three) against only three A supply points.
        before = availability(self.db)
        with self.assertRaises(ReservationRejected):
            reserve_order(self.db, order_payload(3, [
                {'line_no': 0, 'candidates': ['A'], 'quantity': 1,
                 'due_period': 3},
                {'line_no': 1, 'candidates': ['A'], 'quantity': 1,
                 'due_period': 3},
                {'line_no': 2, 'candidates': ['A'], 'quantity': 1,
                 'due_period': 3}]))
        # Refusal changes nothing: head version, orders and allocations.
        self.assertEqual(availability(self.db), before)

        # A single extra A-only unit *is* feasible once order 1 yields one A
        # unit and replaces it from B -- this is exactly why per-order greedy
        # allocation would be wrong.  The new order gets the freed A point.
        extra = reserve_order(self.db, order_payload(4, [
            {'line_no': 0, 'candidates': ['A'], 'quantity': 1,
             'due_period': 3}]))
        self.assertTrue(all(r['item'] == 'A' for r in extra['allocations']))
        # Every active line still holds its full promised quantity.
        conn = connect(self.db)
        try:
            totals = {}
            for row in conn.execute('SELECT order_id, line_no, SUM(quantity) q '
                                    'FROM sales_allocation GROUP BY order_id, line_no'):
                totals[(row['order_id'], row['line_no'])] = row['q']
        finally:
            conn.close()
        self.assertEqual(totals, {(1, 0): 2, (2, 0): 1, (4, 0): 1})
        # And no supply point is ever over-committed.
        for row in availability(self.db)['supply']:
            self.assertLessEqual(
                row['committed'],
                row['quantity'] - row['version_requirement_consumed'])

    def test_released_id_is_never_reused_and_replay_does_not_revive(self):
        reserve_order(self.db, ORDER_AB)
        release_order(self.db, ORDER_AB['order_id'])
        # The row stays in released state and the id cannot create a new order,
        # even with identical content.
        with self.assertRaises(DuplicateOrder):
            reserve_order(self.db, ORDER_AB)
        conn = connect(self.db)
        try:
            row = conn.execute(
                'SELECT status FROM sales_order WHERE order_id = 1').fetchone()
            self.assertEqual(row['status'], 'released')
            self.assertEqual(
                conn.execute('SELECT COUNT(*) c FROM sales_allocation').fetchone()['c'], 0)
        finally:
            conn.close()

    def test_release_redistributes_remaining_orders_jointly(self):
        reserve_order(self.db, ORDER_AB)
        reserve_order(self.db, ORDER_A)
        result = release_order(self.db, 1)
        # Order 2 (A-only) now reallocates onto the lexicographically earliest
        # A supply instead of keeping its old t2 placement.
        self.assertEqual(
            [(r['item'], r['supply_period'], r['quantity'])
             for r in result['allocations']],
            [('A', 0, 1)])
        av = availability(self.db)
        self.assertEqual(
            [(r['item'], r['supply_period'], r['remaining']) for r in av['supply']],
            [('A', 0, 0), ('A', 1, 1), ('A', 2, 1), ('B', 2, 5)])
        with self.assertRaisesRegex(RevisionError, 'already released'):
            release_order(self.db, 1)
        with self.assertRaisesRegex(RevisionError, 'unknown order'):
            release_order(self.db, 999)

    def test_availability_reports_per_supply_remaining(self):
        reserve_order(self.db, ORDER_AB)
        av = availability(self.db)
        self.assertEqual(av['revision']['version'], 0)
        self.assertEqual(av['active_order_ids'], [1])
        self.assertEqual(
            [(r['item'], r['supply_period'], r['supply_kind'], r['quantity'],
              r['committed'], r['remaining']) for r in av['supply']],
            [('A', 0, 'on_hand', 1, 1, 0),
             ('A', 1, 'scheduled_receipt', 1, 1, 0),
             ('A', 2, 'scheduled_receipt', 1, 0, 1),
             ('B', 2, 'scheduled_receipt', 5, 0, 5)])
        # Conservation: quantity == version-consumed + committed + remaining.
        for row in av['supply']:
            self.assertEqual(
                row['quantity'],
                row['version_requirement_consumed'] + row['committed']
                + row['remaining'])

    def test_payload_validation(self):
        bad_payloads = [
            {'lines': []},
            {'order_id': '1', 'lines': ORDER_AB['lines']},
            {'order_id': -1, 'lines': ORDER_AB['lines']},
            {'order_id': 1, 'lines': []},
            {'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': [], 'quantity': 1, 'due_period': 3}]},
            {'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': ['A'], 'quantity': 0, 'due_period': 3}]},
            {'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': ['A'], 'quantity': 1, 'due_period': 99}]},
            {'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': ['P'], 'quantity': 1, 'due_period': 3}]},
            {'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': ['GHOST'], 'quantity': 1, 'due_period': 3}]},
            {'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': ['A', 'A'], 'quantity': 1,
                 'due_period': 3}]},
            {'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': ['A'], 'quantity': 1, 'due_period': 3},
                {'line_no': 0, 'candidates': ['B'], 'quantity': 1, 'due_period': 3}]},
        ]
        for payload in bad_payloads:
            with self.assertRaises(RevisionError):
                reserve_order(self.db, payload)

    def test_concurrent_reservations_leave_no_half_order(self):
        errors = []

        def worker(order_id):
            try:
                reserve_order(self.db, order_payload(order_id, [
                    {'line_no': 0, 'candidates': ['A'], 'quantity': 1,
                     'due_period': 3}]))
            except (ReservationRejected, BatchConflict, sqlite3.OperationalError) as error:
                errors.append((order_id, type(error).__name__))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10, 14)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Only three A units exist, so exactly three reservations commit and
        # one is refused; every committed order is fully allocated.
        conn = connect(self.db)
        try:
            active = conn.execute(
                "SELECT COUNT(*) c FROM sales_order WHERE status = 'active'").fetchone()['c']
            allocated = conn.execute(
                'SELECT COALESCE(SUM(quantity),0) q FROM sales_allocation').fetchone()['q']
        finally:
            conn.close()
        self.assertEqual(active, 3)
        self.assertEqual(allocated, 3)
        self.assertEqual(len(errors), 1)

    def test_termination_before_commit_leaves_no_half_order(self):
        def kill(_conn):
            raise SystemExit('simulated termination')

        with self.assertRaises(SystemExit):
            reserve_order(self.db, ORDER_AB, crash_hook=kill)
        conn = connect(self.db)
        try:
            self.assertEqual(
                conn.execute('SELECT COUNT(*) c FROM sales_order').fetchone()['c'], 0)
            self.assertEqual(
                conn.execute('SELECT COUNT(*) c FROM sales_allocation').fetchone()['c'], 0)
            gen = conn.execute(
                "SELECT value FROM meta WHERE key = 'allocation_generation'").fetchone()['value']
            self.assertEqual(gen, '0')
        finally:
            conn.close()
        # The same order can then be committed cleanly.
        result = reserve_order(self.db, ORDER_AB)
        self.assertFalse(result['replayed'])


class RevisionInteractionTests(unittest.TestCase):
    # The headline re-allocation scenario: A has 1 on hand plus one receipt at
    # t1; B has a receipt at t2.  With two A units promised first, a later
    # A-only order can still be placed (it takes B via its candidate set only
    # after an A arrival is withdrawn).
    DATA = {
        'periods': 6,
        'items': [
            {'id': 'P', 'lead_time': 1, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0},
            {'id': 'A', 'lead_time': 1, 'lot_size': 1, 'on_hand': 1, 'safety_stock': 0},
            {'id': 'B', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0}],
        'bom': [{'parent': 'P', 'component': 'A', 'quantity': 1}],
        'demand': [],
        'receipts': [
            {'item': 'A', 'period': 1, 'quantity': 1},
            {'item': 'B', 'period': 2, 'quantity': 5}]}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'rev.db'
        initialize(self.db, self.DATA, 'c' * 64)

    def tearDown(self):
        self.temp.cleanup()

    def _allocation_map(self):
        conn = connect(self.db)
        try:
            return {(r['order_id'], r['line_no'], r['item_id'],
                     r['supply_period'], r['supply_kind']): r['quantity']
                    for r in conn.execute('SELECT * FROM sales_allocation')}
        finally:
            conn.close()

    def test_withdraw_arrival_forces_reallocation_then_withdraw_b_is_rejected(self):
        # Order 1 ({A,B}, qty 1) takes the on-hand A unit first; order 2
        # (A-only, qty 1) takes the t1 A receipt.  Withdrawing that receipt
        # forces order 1 onto B while order 2 keeps the remaining A.
        reserve_order(self.db, {
            'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': ['A', 'B'], 'quantity': 1,
                 'due_period': 3}]})
        reserve_order(self.db, {
            'order_id': 2, 'lines': [
                {'line_no': 0, 'candidates': ['A'], 'quantity': 1,
                 'due_period': 3}]})
        mapping = self._allocation_map()
        self.assertEqual(mapping.get((1, 0, 'A', 0, 'on_hand')), 1)
        self.assertEqual(mapping.get((2, 0, 'A', 1, 'scheduled_receipt')), 1)

        # Withdraw the A receipt at t1: order 2 (A-only) keeps its promise on
        # the on-hand unit and order 1 moves onto the B t2 receipt.
        version, _ = apply_batch(
            self.db, 'withdraw-a1', 0,
            {'receipts': [{'item': 'A', 'period': 1, 'delta': -1}]})
        self.assertEqual(version, 1)
        mapping = self._allocation_map()
        self.assertEqual(mapping.get((2, 0, 'A', 0, 'on_hand')), 1)
        self.assertEqual(mapping.get((1, 0, 'B', 2, 'scheduled_receipt')), 1)
        self.assertNotIn((1, 0, 'A', 1, 'scheduled_receipt'), mapping)
        self.assertNotIn((2, 0, 'A', 1, 'scheduled_receipt'), mapping)
        # The history keeps every allocation generation, bound to plan versions.
        conn = connect(self.db)
        try:
            generations = [r['generation'] for r in conn.execute(
                'SELECT DISTINCT generation FROM sales_allocation_history '
                'ORDER BY generation')]
            bound = [r['plan_version'] for r in conn.execute(
                'SELECT DISTINCT plan_version FROM sales_allocation_history '
                'ORDER BY plan_version')]
        finally:
            conn.close()
        self.assertEqual(generations, [1, 2, 3])
        self.assertEqual(bound, [0, 1])

        # Now remove the B receipt as well: order 1's B unit has nowhere to go
        # (its only other candidate A has a single on-hand unit already shared
        # with order 2), so the entire revision is rejected -- head and
        # allocations stay exactly at their committed state.
        before_head = load_report(self.db)['revision']['version']
        before_mapping = self._allocation_map()
        with self.assertRaises(ReservationRejected):
            apply_batch(
                self.db, 'withdraw-b', 1,
                {'receipts': [{'item': 'B', 'period': 2, 'delta': -5}]})
        self.assertEqual(load_report(self.db)['revision']['version'], before_head)
        self.assertEqual(self._allocation_map(), before_mapping)
        # Release order 1 first: the revision becomes committable.
        release_order(self.db, 1)
        version, _ = apply_batch(
            self.db, 'withdraw-b', 1,
            {'receipts': [{'item': 'B', 'period': 2, 'delta': -5}]})
        self.assertEqual(version, 2)
        av = availability(self.db)
        self.assertTrue(all(row['item'] != 'B' or row['remaining'] == 0
                            for row in av['supply']))

    def test_demand_delta_consuming_supply_reallocates_in_same_transaction(self):
        reserve_order(self.db, order_payload(5, [
            {'line_no': 0, 'candidates': ['A'], 'quantity': 1,
             'due_period': 3}]))
        # Independent A demand at t0 takes the on-hand unit via version FIFO;
        # the order must move to the t1 receipt.
        version, _ = apply_batch(
            self.db, 'demand-a0', 0,
            {'demand': [{'item': 'A', 'period': 0, 'delta': 1}]})
        self.assertEqual(version, 1)
        mapping = self._allocation_map()
        self.assertNotIn((5, 0, 'A', 0, 'on_hand'), mapping)
        self.assertEqual(mapping.get((5, 0, 'A', 1, 'scheduled_receipt')), 1)

    def test_rejected_revision_changes_nothing(self):
        reserve_order(self.db, {
            'order_id': 1, 'lines': [
                {'line_no': 0, 'candidates': ['A', 'B'], 'quantity': 2,
                 'due_period': 3}]})
        before = availability(self.db)
        with self.assertRaises(ReservationRejected):
            apply_batch(
                self.db, 'starve', 0,
                {'receipts': [
                    {'item': 'A', 'period': 1, 'delta': -1},
                    {'item': 'B', 'period': 2, 'delta': -5}]})
        self.assertEqual(availability(self.db), before)
        self.assertEqual(load_report(self.db)['revision']['version'], 0)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _legacy_schema_database(self):
        """Build a v1-schema database by stripping the v2 tables post-init."""
        db = self.dir / 'legacy.db'
        initialize(db, DATA, 'd' * 64)
        conn = connect(db)
        try:
            conn.execute('BEGIN IMMEDIATE')
            for table in ('sales_allocation_history', 'sales_allocation',
                          'sales_order_candidate', 'sales_order_line',
                          'sales_order'):
                conn.execute('DROP TABLE ' + table)
            conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
            conn.execute("DELETE FROM meta WHERE key = 'allocation_generation'")
            conn.commit()
        finally:
            conn.close()
        return db

    def test_legacy_database_reports_and_migrates(self):
        db = self._legacy_schema_database()
        # Old report path keeps working on the unmigrated database.
        report = load_report(db)
        self.assertEqual(report['schema_version'], SCHEMA_VERSION)
        # Availability works read-only without migrating the file.
        av = availability(db)
        self.assertEqual(av['allocation_generation'], 0)
        self.assertEqual(av['active_order_ids'], [])
        conn = connect(db, readonly=True)
        try:
            self.assertFalse(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sales_order'").fetchone())
        finally:
            conn.close()
        # The first commitment write migrates the schema inside its transaction.
        result = reserve_order(db, ORDER_AB)
        self.assertFalse(result['replayed'])
        conn = connect(db)
        try:
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()['value']
            self.assertEqual(int(version), SCHEMA_VERSION)
            self.assertTrue(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sales_allocation_history'").fetchone())
        finally:
            conn.close()
        # Historical plan data survived the migration; the planning order is
        # the BOM topological order (indegree-zero leaves first, sorted by id,
        # then their parent).
        self.assertEqual(load_report(db)['result']['planning_order'],
                         ['B', 'P', 'A'])

    def test_apply_on_legacy_database_migrates_before_commitment_check(self):
        db = self._legacy_schema_database()
        # No active orders, so the revision applies and migrates atomically.
        version, _ = apply_batch(
            db, 'b1', 0, {'demand': [{'item': 'A', 'period': 4, 'delta': 2}]})
        self.assertEqual(version, 1)
        report = load_report(db)
        self.assertEqual(report['revision']['version'], 1)
        conn = connect(db)
        try:
            self.assertTrue(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sales_order'").fetchone())
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()


class AllocatorPropertyTests(unittest.TestCase):
    """Brute-force cross-check of the allocator on small random instances.

    Every feasible instance is enumerated exhaustively for all integer
    allocations that honour candidate sets, due dates and point capacities;
    the allocator's output must (a) be one of them, (b) conserve every supply
    point, and (c) equal the lexicographically greatest vector under the fixed
    (order_id, line_no, item, supply_period, kind) variable ordering.
    """

    def _brute_force_vectors(self, points, orders):
        # Variables in the same order the allocator uses.
        variables = []
        line_needs = []
        for order_entry in sorted(orders, key=lambda o: o['order_id']):
            for line in sorted(order_entry['lines'], key=lambda l: l['line_no']):
                eligible = [idx for idx, (item, period, _k, _q) in enumerate(points)
                            if item in line['candidates'] and period <= line['due_period']]
                for idx in eligible:
                    variables.append((order_entry['order_id'], line['line_no'], idx))
                line_needs.append((order_entry['order_id'], line['line_no'],
                                   line['quantity'], eligible))
        capacities = [point[3] for point in points]
        results = []
        current = [0] * len(variables)

        def enumerate_vectors(pos, remaining_cap):
            if pos == len(variables):
                if all(need == 0 for _o, _l, need, _e in line_needs):
                    results.append(tuple(current))
                return
            order_id, line_no, idx = variables[pos]
            need_entry = next(e for e in line_needs
                              if e[0] == order_id and e[1] == line_no)
            need_index = line_needs.index(need_entry)
            # Variables of one line are contiguous; cap by line need.
            cap = min(remaining_cap[idx], need_entry[2])
            for amount in range(cap + 1):
                current[pos] = amount
                line_needs[need_index] = (order_id, line_no,
                                          need_entry[2] - amount, need_entry[3])
                remaining_cap[idx] -= amount
                enumerate_vectors(pos + 1, remaining_cap)
                remaining_cap[idx] += amount
                line_needs[need_index] = need_entry
            current[pos] = 0

        enumerate_vectors(0, list(capacities))
        # Keep only vectors that exactly satisfy every line.
        valid = []
        for vector in results:
            line_totals = {}
            for position, value in enumerate(vector):
                order_id, line_no, _idx = variables[position]
                line_totals[(order_id, line_no)] = line_totals.get(
                    (order_id, line_no), 0) + value
            if all(line_totals.get((o, l), 0) == need
                   for o, l, need, _e in line_needs):
                valid.append(vector)
        return variables, valid

    def test_random_instances_match_brute_force_lex_max(self):
        import random
        rng = random.Random(20261115)
        for trial in range(40):
            periods = rng.randint(2, 4)
            leaf_names = ['A', 'B']
            items = {name: {'lead_time': 0, 'lot_size': 1,
                            'on_hand': rng.randint(0, 2), 'safety_stock': 0}
                     for name in leaf_names}
            bom = {name: [] for name in leaf_names}
            order = list(leaf_names)
            demand = {name: [0] * periods for name in leaf_names}
            receipts = {name: [0] * periods for name in leaf_names}
            for name in leaf_names:
                for t in range(1, periods):
                    if rng.random() < 0.5:
                        receipts[name][t] = rng.randint(1, 2)
            order_count = rng.randint(1, 3)
            orders = []
            for order_id in range(order_count):
                line_count = rng.randint(1, 2)
                lines = []
                for line_no in range(line_count):
                    candidates = rng.sample(
                        leaf_names, rng.randint(1, len(leaf_names)))
                    lines.append({
                        'line_no': line_no,
                        'candidates': tuple(sorted(candidates)),
                        'quantity': rng.randint(1, 3),
                        'due_period': rng.randint(0, periods - 1)})
                orders.append({'order_id': order_id, 'lines': lines})
            points = build_supply_points(periods, items, receipts, set(leaf_names))
            variables, valid = self._brute_force_vectors(points, orders)
            try:
                outcome = allocate_commitments(
                    periods, items, bom, demand, receipts, order, orders)
            except CommitmentInfeasible:
                self.assertEqual(valid, [], f'trial {trial}: false infeasible')
                continue
            self.assertTrue(valid, f'trial {trial}: false feasible')
            # Reconstruct the allocator's vector in the brute-force order.
            placed = {(r['order_id'], r['line_no'], r['item'],
                       r['supply_period'],
                       0 if r['supply_kind'] == 'on_hand' else 1): r['quantity']
                      for r in outcome['allocations']}
            vector = []
            for order_id, line_no, idx in variables:
                item, period, kind, _q = points[idx]
                vector.append(placed.get((order_id, line_no, item, period, kind), 0))
            vector = tuple(vector)
            self.assertIn(vector, valid, f'trial {trial}: not a feasible vector')
            self.assertEqual(vector, max(valid),
                             f'trial {trial}: not lexicographically greatest')
            # Supply conservation against the post-FIFO residual.
            gross = leaf_gross_requirements(
                periods, items, bom, demand, receipts, order, set(leaf_names))
            residual, _short = fifo_version_consumption(
                points, periods, set(leaf_names), gross)
            used = {key: 0 for key in residual}
            for row in outcome['allocations']:
                kind = 0 if row['supply_kind'] == 'on_hand' else 1
                used[(row['item'], row['supply_period'], kind)] += row['quantity']
            for key, value in used.items():
                self.assertLessEqual(value, residual[key])
            self.assertEqual(outcome['residual'],
                             {key: residual[key] - used[key] for key in residual})


if __name__ == '__main__':
    unittest.main()
