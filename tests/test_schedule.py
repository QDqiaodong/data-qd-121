"""Finite-capacity scheduling tests.

Covers the required behaviours: a greedy failure must not be reported as
infeasible, shared-component contention, resource downtime, same-period
supply, scheduling a historical version after revisions, input errors
preserving the old output, and micro instances cross-checked against an
independent exhaustive enumeration written directly in this file.
"""
import itertools
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from material_plan.schedule import schedule_version
from material_plan.store import (apply_batch, initialize, list_versions,
                                 load_report, load_schedule_source)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'input.json'


def make_source(periods, items, bom, demand=None, receipts=None, planned=None):
    """Build a scheduler source dict directly (no store involved)."""
    names = [row['id'] for row in items]
    policies = {row['id']: {key: row[key] for key in
                            ('lead_time', 'lot_size', 'on_hand', 'safety_stock')}
                for row in items}
    bom_map = {name: [] for name in names}
    for edge in bom:
        bom_map[edge['parent']].append((edge['component'], edge['quantity']))

    def matrix(rows):
        result = {name: [0] * periods for name in names}
        for item, period, quantity in rows or []:
            result[item][period] = quantity
        return result

    return {'periods': periods, 'items': policies, 'bom': bom_map,
            'order': list(names), 'demand': matrix(demand),
            'receipts': matrix(receipts),
            'planned_receipts': matrix(planned)}


def calendar(jobs, capacities, periods):
    return {'jobs': [{'item': item, 'resource': resource, 'duration': duration}
                     for item, resource, duration in jobs],
            'resources': [{'id': resource, 'capacity': capacity}
                          for resource, capacity in capacities.items()]}


def starts_of(result):
    if result['status'] != 'scheduled':
        return None
    return tuple(job['start'] for job in result['jobs'])


# ---------------------------------------------------------------------------
# Independent exhaustive enumeration, written separately from the solver.
# ---------------------------------------------------------------------------

def brute_force(source, calendar_data):
    """Try every start combination; return the lexicographically smallest
    feasible vector in (receipt_period, item) job order, or None."""
    periods = source['periods']
    bom = source['bom']
    jobs = []
    for item in source['order']:
        if not bom[item]:
            continue
        for period, quantity in enumerate(source['planned_receipts'][item]):
            if quantity:
                jobs.append((item, period, quantity))
    jobs.sort(key=lambda job: (job[1], job[0]))
    config = {row['item']: row for row in calendar_data['jobs']}
    capacity = {row['id']: row['capacity'] for row in calendar_data['resources']}
    domains = []
    for item, due, _quantity in jobs:
        duration = config[item]['duration']
        domains.append(range(0, due - duration + 1))
    best = None
    for combo in itertools.product(*domains):
        if _feasible(source, jobs, combo, config, capacity):
            combo = tuple(combo)
            if best is None or combo < best:
                best = combo
    return best


def _feasible(source, jobs, starts, config, capacity):
    periods = source['periods']
    bom = source['bom']
    used = {resource: [0] * periods for resource in capacity}
    completions = {item: [0] * periods for item in source['items']}
    consumption = {item: [0] * periods for item in source['items']}
    for (item, _due, quantity), start in zip(jobs, starts):
        resource = config[item]['resource']
        duration = config[item]['duration']
        for p in range(start, start + duration):
            used[resource][p] += 1
            if used[resource][p] > capacity[resource][p]:
                return False
        completions[item][start + duration] += quantity
        for component, per in bom[item]:
            consumption[component][start] += quantity * per
    for item in source['order']:
        balance = source['items'][item]['on_hand']
        leaf = not bom[item]
        for t in range(periods):
            balance += source['receipts'][item][t]
            if leaf:
                balance += source['planned_receipts'][item][t]
            balance += completions[item][t]
            balance -= consumption[item][t]
            if balance < 0:
                return False
            balance -= source['demand'][item][t]
            if balance < 0:
                return False
    return True


def greedy_one_pass(source, calendar_data):
    """Naive greedy: schedule jobs in (due, item) order at the earliest start
    that works against already-placed jobs only; unscheduled jobs provide no
    supply.  Returns None when it gets stuck."""
    periods = source['periods']
    bom = source['bom']
    jobs = []
    for item in source['order']:
        if not bom[item]:
            continue
        for period, quantity in enumerate(source['planned_receipts'][item]):
            if quantity:
                jobs.append((item, period, quantity))
    jobs.sort(key=lambda job: (job[1], job[0]))
    config = {row['item']: row for row in calendar_data['jobs']}
    capacity = {row['id']: row['capacity'] for row in calendar_data['resources']}
    placed = []
    for index, (item, due, _quantity) in enumerate(jobs):
        duration = config[item]['duration']
        chosen = None
        for start in range(0, due - duration + 1):
            trial = placed + [start]
            if _feasible(source, jobs[:index + 1], trial, config, capacity):
                chosen = start
                break
        if chosen is None:
            return None
        placed.append(chosen)
    return tuple(placed)


class GreedyFailureTests(unittest.TestCase):
    """A consumer is due before its producer: one-pass greedy in (due, item)
    order sees no supply for TOP and gets stuck, but a feasible schedule
    exists once MID is placed to complete exactly when TOP starts."""

    def setUp(self):
        self.source = make_source(
            periods=6,
            items=[{'id': 'TOP', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'MID', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'RAW', 'lead_time': 0, 'lot_size': 1, 'on_hand': 10,
                    'safety_stock': 0}],
            bom=[{'parent': 'TOP', 'component': 'MID', 'quantity': 1},
                 {'parent': 'MID', 'component': 'RAW', 'quantity': 1}],
            planned=[('TOP', 4, 1), ('MID', 5, 1)])
        self.calendar = calendar(
            [('TOP', 'line', 1), ('MID', 'line', 2)],
            {'line': [1, 1, 1, 1, 1, 1]}, 6)

    def test_greedy_gets_stuck(self):
        self.assertIsNone(greedy_one_pass(self.source, self.calendar))

    def test_complete_search_finds_the_schedule(self):
        result = schedule_version(self.source, self.calendar)
        self.assertEqual(result['status'], 'scheduled')
        jobs = {job['item']: job for job in result['jobs']}
        # TOP can only start once MID (duration 2) has completed.
        self.assertEqual(jobs['TOP']['start'], 2)
        self.assertEqual(jobs['MID']['start'], 0)
        self.assertEqual(jobs['MID']['completion'], 2)
        self.assertEqual(jobs['TOP']['completion'], 3)
        self.assertEqual(starts_of(result),
                         brute_force(self.source, self.calendar))


class SharedContentionTests(unittest.TestCase):
    """Two parents compete for one shared purchased component: the initial
    unit covers the first job, the second must wait for the period-2 receipt."""

    def setUp(self):
        self.source = make_source(
            periods=5,
            items=[{'id': 'A', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'B', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'C', 'lead_time': 0, 'lot_size': 1, 'on_hand': 1,
                    'safety_stock': 0}],
            bom=[{'parent': 'A', 'component': 'C', 'quantity': 1},
                 {'parent': 'B', 'component': 'C', 'quantity': 1}],
            receipts=[('C', 2, 1)],
            planned=[('A', 2, 1), ('B', 3, 1)])
        self.calendar = calendar(
            [('A', 'ra', 1), ('B', 'rb', 1)],
            {'ra': [1] * 5, 'rb': [1] * 5}, 5)

    def test_contention_resolved_by_receipt_timing(self):
        result = schedule_version(self.source, self.calendar)
        self.assertEqual(result['status'], 'scheduled')
        jobs = {job['item']: job for job in result['jobs']}
        self.assertEqual(jobs['A']['start'], 0)   # consumes the stocked unit
        self.assertEqual(jobs['B']['start'], 2)   # waits for the receipt
        ledger = next(row for row in result['inventory'] if row['item'] == 'C')
        self.assertTrue(all(row['projected_available'] >= 0
                            for row in ledger['periods']))
        self.assertEqual(ledger['periods'][0]['job_consumption'], 1)
        self.assertEqual(ledger['periods'][2]['scheduled_receipt'], 1)
        self.assertEqual(ledger['periods'][2]['job_consumption'], 1)
        self.assertEqual(starts_of(result),
                         brute_force(self.source, self.calendar))

    def test_insufficient_shared_stock_is_proven_infeasible(self):
        source = make_source(
            periods=5,
            items=[{'id': 'A', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'B', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'C', 'lead_time': 0, 'lot_size': 1, 'on_hand': 1,
                    'safety_stock': 0}],
            bom=[{'parent': 'A', 'component': 'C', 'quantity': 1},
                 {'parent': 'B', 'component': 'C', 'quantity': 1}],
            planned=[('A', 2, 1), ('B', 3, 1)])
        result = schedule_version(source, self.calendar)
        self.assertEqual(result['status'], 'infeasible')
        self.assertIsNone(brute_force(source, self.calendar))
        self.assertEqual(result['inventory'], [])
        self.assertTrue(all(job['start'] is None for job in result['jobs']))


class DowntimeTests(unittest.TestCase):
    def setUp(self):
        self.source = make_source(
            periods=5,
            items=[{'id': 'P', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'M', 'lead_time': 0, 'lot_size': 1, 'on_hand': 10,
                    'safety_stock': 0}],
            bom=[{'parent': 'P', 'component': 'M', 'quantity': 1}],
            planned=[('P', 4, 1)])

    def test_job_slides_past_the_down_period(self):
        cal = calendar([('P', 'line', 2)], {'line': [1, 0, 1, 1, 1]}, 5)
        result = schedule_version(self.source, cal)
        self.assertEqual(result['status'], 'scheduled')
        job = result['jobs'][0]
        self.assertEqual((job['start'], job['completion']), (2, 4))
        usage = result['resource_usage'][0]['periods']
        self.assertEqual([row['used'] for row in usage], [0, 0, 1, 1, 0])
        self.assertEqual(starts_of(result), brute_force(self.source, cal))

    def test_full_downtime_is_structured_infeasible(self):
        cal = calendar([('P', 'line', 2)], {'line': [0, 0, 0, 0, 0]}, 5)
        result = schedule_version(self.source, cal)
        self.assertEqual(result['status'], 'infeasible')
        self.assertEqual(result['jobs'][0]['start'], None)
        self.assertEqual(result['inventory'], [])
        usage = result['resource_usage'][0]['periods']
        self.assertTrue(all(row['capacity'] == 0 and row['used'] == 0
                            for row in usage))
        self.assertIsNone(brute_force(self.source, cal))

    def test_completion_before_duration_is_infeasible(self):
        source = make_source(
            periods=5,
            items=[{'id': 'P', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'M', 'lead_time': 0, 'lot_size': 1, 'on_hand': 10,
                    'safety_stock': 0}],
            bom=[{'parent': 'P', 'component': 'M', 'quantity': 1}],
            planned=[('P', 1, 1)])
        cal = calendar([('P', 'line', 2)], {'line': [1] * 5}, 5)
        result = schedule_version(source, cal)
        self.assertEqual(result['status'], 'infeasible')
        self.assertIsNone(brute_force(source, cal))


class SamePeriodSupplyTests(unittest.TestCase):
    def test_scheduled_receipt_supplies_same_period_start(self):
        source = make_source(
            periods=4,
            items=[{'id': 'P', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'C', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0}],
            bom=[{'parent': 'P', 'component': 'C', 'quantity': 5}],
            receipts=[('C', 1, 5)],
            planned=[('P', 2, 1)])
        cal = calendar([('P', 'line', 1)], {'line': [1] * 4}, 4)
        result = schedule_version(source, cal)
        self.assertEqual(result['status'], 'scheduled')
        # Start 0 sees no stock; the period-1 receipt lands before starts.
        self.assertEqual(result['jobs'][0]['start'], 1)
        ledger = next(row for row in result['inventory'] if row['item'] == 'C')
        self.assertEqual(ledger['periods'][1]['scheduled_receipt'], 5)
        self.assertEqual(ledger['periods'][1]['job_consumption'], 5)
        self.assertEqual(ledger['periods'][1]['projected_available'], 0)
        self.assertEqual(starts_of(result), brute_force(source, cal))

    def test_completion_supplies_same_period_start(self):
        # MID completes at period 2; TOP starts at period 2 and consumes it.
        source = make_source(
            periods=5,
            items=[{'id': 'TOP', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'MID', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'RAW', 'lead_time': 0, 'lot_size': 1, 'on_hand': 4,
                    'safety_stock': 0}],
            bom=[{'parent': 'TOP', 'component': 'MID', 'quantity': 2},
                 {'parent': 'MID', 'component': 'RAW', 'quantity': 1}],
            planned=[('TOP', 3, 1), ('MID', 4, 2)])
        cal = calendar([('TOP', 'line', 1), ('MID', 'line', 2)],
                       {'line': [1] * 5}, 5)
        result = schedule_version(source, cal)
        self.assertEqual(result['status'], 'scheduled')
        jobs = {job['item']: job for job in result['jobs']}
        self.assertEqual(jobs['MID']['completion'], 2)
        self.assertEqual(jobs['TOP']['start'], 2)
        self.assertEqual(starts_of(result), brute_force(source, cal))


class ExhaustiveCrossCheckTests(unittest.TestCase):
    """Random micro instances: the solver must agree with the independent
    brute-force enumeration above, on feasibility and on the exact vector."""

    def test_random_micro_instances_match_brute_force(self):
        rng = random.Random(20261115)
        scheduled = infeasible = 0
        for trial in range(40):
            with self.subTest(trial=trial):
                source, cal = self._instance(rng)
                result = schedule_version(source, cal)
                expected = brute_force(source, cal)
                self.assertEqual(starts_of(result), expected)
                if expected is None:
                    infeasible += 1
                    self.assertEqual(result['status'], 'infeasible')
                else:
                    scheduled += 1
                    self.assertEqual(result['status'], 'scheduled')
                    self._assert_ledger_balances(source, result)
        self.assertGreater(scheduled, 0)
        self.assertGreater(infeasible, 0)

    def _instance(self, rng):
        periods = rng.randint(4, 7)
        items = [{'id': name, 'lead_time': 0, 'lot_size': 1,
                  'on_hand': rng.randint(0, 3), 'safety_stock': 0}
                 for name in ('A', 'B', 'C')]
        bom = [{'parent': 'A', 'component': 'B', 'quantity': rng.randint(1, 2)},
               {'parent': 'A', 'component': 'C', 'quantity': 1},
               {'parent': 'B', 'component': 'C', 'quantity': rng.randint(1, 2)}]
        planned = []
        for item in ('A', 'B'):
            for period in range(1, periods):
                if rng.random() < 0.3:
                    planned.append((item, period, rng.randint(1, 3)))
        jobs_count = len(planned)
        if not 1 <= jobs_count <= 4:
            return self._instance(rng)
        demand = [(item, rng.randrange(periods), rng.randint(0, 2))
                  for item in ('A', 'B', 'C') if rng.random() < 0.4]
        receipts = [('C', rng.randrange(periods), rng.randint(1, 3))
                    for _ in range(rng.randint(0, 2))]
        leaf_planned = [('C', rng.randrange(periods), rng.randint(1, 3))
                        for _ in range(rng.randint(0, 2))]
        source = make_source(periods, items, bom, demand=demand,
                             receipts=receipts, planned=planned + leaf_planned)
        capacities = {}
        for resource in ('r1', 'r2'):
            capacities[resource] = [1 if rng.random() < 0.8 else 0
                                    for _ in range(periods)]
        cal = calendar(
            [('A', 'r1', rng.randint(1, 3)), ('B', 'r2', rng.randint(1, 3))],
            capacities, periods)
        return source, cal

    def _assert_ledger_balances(self, source, result):
        for plan in result['inventory']:
            previous = source['items'][plan['item']]['on_hand']
            for row in plan['periods']:
                self.assertEqual(row['opening_available'], previous)
                self.assertEqual(
                    row['projected_available'],
                    row['opening_available'] + row['scheduled_receipt']
                    + row['planned_receipt'] + row['completions']
                    - row['job_consumption'] - row['independent_demand'])
                self.assertGreaterEqual(row['projected_available'], 0)
                previous = row['projected_available']
        for usage in result['resource_usage']:
            for row in usage['periods']:
                self.assertLessEqual(row['used'], row['capacity'])


class StoreVersionTests(unittest.TestCase):
    """Scheduling reads any committed revision and never writes the store."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.db = self.dir / 'plan.db'
        initialize(self.db, json.loads(FIXTURE.read_text()), 'a' * 64)
        apply_batch(self.db, 'b1', 0,
                    {'demand': [{'item': 'P', 'period': 3, 'delta': 2}]})
        self.calendar = calendar([('P', 'line', 1)], {'line': [1] * 4}, 4)

    def tearDown(self):
        self.temp.cleanup()

    def test_old_version_scheduled_after_revision(self):
        old = schedule_version(load_schedule_source(self.db, 0), self.calendar)
        head = schedule_version(load_schedule_source(self.db), self.calendar)
        self.assertEqual(old['jobs'][0]['quantity'], 3)
        self.assertEqual(head['jobs'][0]['quantity'], 5)
        self.assertEqual(old['jobs'][0]['start'], 2)
        self.assertEqual(head['jobs'][0]['start'], 2)
        source = load_schedule_source(self.db, 0)
        self.assertEqual(source['version'], 0)
        self.assertEqual(source['head_version'], 1)

    def test_scheduling_never_modifies_the_store(self):
        before = [dict(row) for row in list_versions(self.db)]
        head_before = load_report(self.db)['revision']['head_version']
        schedule_version(load_schedule_source(self.db, 0), self.calendar)
        schedule_version(load_schedule_source(self.db), self.calendar)
        self.assertEqual([dict(row) for row in list_versions(self.db)], before)
        self.assertEqual(load_report(self.db)['revision']['head_version'],
                         head_before)

    def test_unknown_version_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unknown version'):
            load_schedule_source(self.db, 99)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.source = make_source(
            periods=4,
            items=[{'id': 'P', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                    'safety_stock': 0},
                   {'id': 'M', 'lead_time': 0, 'lot_size': 1, 'on_hand': 9,
                    'safety_stock': 0}],
            bom=[{'parent': 'P', 'component': 'M', 'quantity': 1}],
            planned=[('P', 3, 1)])
        self.good = calendar([('P', 'line', 1)], {'line': [1] * 4}, 4)

    def test_bad_calendars_rejected(self):
        bad = []
        row = {'item': 'P', 'resource': 'line', 'duration': 0}
        bad.append({'jobs': [row], 'resources': self.good['resources']})
        row = {'item': 'P', 'resource': 'line', 'duration': 9}
        bad.append({'jobs': [row], 'resources': self.good['resources']})
        bad.append({'jobs': [{'item': 'P', 'resource': 'line', 'duration': 1}],
                    'resources': [{'id': 'line', 'capacity': [1, 2, 1, 1]}]})
        bad.append({'jobs': [{'item': 'P', 'resource': 'line', 'duration': 1}],
                    'resources': [{'id': 'line', 'capacity': [1, 1, 1]}]})
        bad.append({'jobs': [{'item': 'ghost', 'resource': 'line', 'duration': 1}],
                    'resources': self.good['resources']})
        bad.append({'jobs': [{'item': 'M', 'resource': 'line', 'duration': 1}],
                    'resources': self.good['resources']})
        bad.append({'jobs': [{'item': 'P', 'resource': 'ghost', 'duration': 1}],
                    'resources': self.good['resources']})
        bad.append({'jobs': [], 'resources': self.good['resources']})
        bad.append({'jobs': [{'item': 'P', 'resource': 'line', 'duration': 1}]})
        bad.append({'jobs': [{'item': 'P', 'resource': 'line', 'duration': 1,
                              'extra': 1}], 'resources': self.good['resources']})
        bad.append({'jobs': [{'item': 'P', 'resource': 'line', 'duration': 1},
                             {'item': 'P', 'resource': 'line', 'duration': 2}],
                    'resources': self.good['resources']})
        bad.append({'jobs': [{'item': 'P', 'resource': 'line', 'duration': 1}],
                    'resources': [{'id': 'line', 'capacity': [1] * 4},
                                  {'id': 'line', 'capacity': [1] * 4}]})
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    schedule_version(self.source, data)
        self.assertEqual(schedule_version(self.source, self.good)['status'],
                         'scheduled')

    def test_period_and_job_limits(self):
        items = [{'id': 'P', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0,
                  'safety_stock': 0},
                 {'id': 'M', 'lead_time': 0, 'lot_size': 1, 'on_hand': 99,
                  'safety_stock': 0}]
        bom = [{'parent': 'P', 'component': 'M', 'quantity': 1}]
        long_source = make_source(33, items, bom, planned=[('P', 32, 1)])
        with self.assertRaisesRegex(ValueError, '32'):
            schedule_version(long_source, self.good)
        many = make_source(14, items, bom,
                           planned=[('P', t, 1) for t in range(1, 14)])
        with self.assertRaisesRegex(ValueError, '12'):
            schedule_version(many, self.good)


class ScheduleCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.db = self.dir / 'plan.db'
        self.cal = self.dir / 'calendar.json'
        self.cal.write_text(json.dumps(calendar(
            [('P', 'line', 1)], {'line': [1] * 4}, 4)))

    def tearDown(self):
        self.temp.cleanup()

    def _cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'material_plan.cli', *map(str, arguments)],
            capture_output=True, text=True)

    def _init(self):
        result = self._cli('init', '--source', FIXTURE, '--database', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_schedule_report_and_stdout(self):
        self._init()
        out = self.dir / 'schedule.json'
        result = self._cli('schedule', '--database', self.db,
                           '--calendar', self.cal, '--version', 0,
                           '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(out.read_text())
        self.assertEqual(json.loads(result.stdout), report)
        self.assertEqual(report['revision'], {'version': 0, 'head_version': 0})
        import hashlib
        self.assertEqual(report['calendar_sha256'],
                         hashlib.sha256(self.cal.read_bytes()).hexdigest())
        self.assertEqual(report['result']['status'], 'scheduled')
        job = report['result']['jobs'][0]
        self.assertEqual({key: job[key] for key in
                          ('item', 'quantity', 'receipt_period', 'start',
                           'completion', 'resource', 'duration')},
                         {'item': 'P', 'quantity': 3, 'receipt_period': 3,
                          'start': 2, 'completion': 3, 'resource': 'line',
                          'duration': 1})
        usage = report['result']['resource_usage'][0]
        self.assertEqual(usage['resource'], 'line')
        self.assertEqual([row['used'] for row in usage['periods']],
                         [0, 0, 1, 0])
        ledger = {row['item']: row for row in report['result']['inventory']}
        self.assertEqual(ledger['P']['periods'][3]['completions'], 3)
        self.assertEqual(ledger['P']['periods'][3]['independent_demand'], 4)
        self.assertEqual(ledger['P']['periods'][3]['projected_available'], 0)
        self.assertEqual(ledger['A']['periods'][2]['job_consumption'], 6)

    def test_infeasible_writes_structured_result_with_exit_zero(self):
        self._init()
        self.cal.write_text(json.dumps(calendar(
            [('P', 'line', 1)], {'line': [0, 0, 0, 0]}, 4)))
        out = self.dir / 'schedule.json'
        result = self._cli('schedule', '--database', self.db,
                           '--calendar', self.cal, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(out.read_text())
        self.assertEqual(report['result']['status'], 'infeasible')
        self.assertEqual(report['result']['jobs'][0]['start'], None)
        self.assertEqual(report['result']['inventory'], [])

    def test_input_errors_preserve_old_output(self):
        self._init()
        out = self.dir / 'schedule.json'
        good = self._cli('schedule', '--database', self.db,
                         '--calendar', self.cal, '--output', out)
        self.assertEqual(good.returncode, 0, good.stderr)
        before = out.read_bytes()
        bad_calendars = [
            {'jobs': [{'item': 'P', 'resource': 'line', 'duration': 9}],
             'resources': [{'id': 'line', 'capacity': [1] * 4}]},
            {'jobs': [{'item': 'P', 'resource': 'line', 'duration': 1}],
             'resources': [{'id': 'line', 'capacity': [1, 0, 1]}]},
            {'jobs': [{'item': 'A', 'resource': 'line', 'duration': 1}],
             'resources': [{'id': 'line', 'capacity': [1] * 4}]},
            {'jobs': [], 'resources': []},
            'not an object',
        ]
        for bad in bad_calendars:
            self.cal.write_text(json.dumps(bad))
            result = self._cli('schedule', '--database', self.db,
                               '--calendar', self.cal, '--output', out)
            self.assertEqual(result.returncode, 2, (bad, result.stderr))
            self.assertEqual(out.read_bytes(), before)
        result = self._cli('schedule', '--database', self.db,
                           '--calendar', self.cal, '--version', 99,
                           '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(out.read_bytes(), before)
        result = self._cli('schedule', '--database', self.dir / 'missing.db',
                           '--calendar', self.cal, '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.dir / 'missing.db').exists())
        self.assertEqual(out.read_bytes(), before)

    def test_too_many_jobs_and_periods_are_input_errors(self):
        data = {'periods': 14,
                'items': [{'id': 'P', 'lead_time': 0, 'lot_size': 1,
                           'on_hand': 0, 'safety_stock': 0},
                          {'id': 'M', 'lead_time': 0, 'lot_size': 1,
                           'on_hand': 99, 'safety_stock': 0}],
                'bom': [{'parent': 'P', 'component': 'M', 'quantity': 1}],
                'demand': [{'item': 'P', 'period': t, 'quantity': 1}
                           for t in range(1, 14)],
                'receipts': []}
        source = self.dir / 'many.json'
        source.write_text(json.dumps(data))
        self._cli('init', '--source', source, '--database', self.db)
        self.cal.write_text(json.dumps(calendar(
            [('P', 'line', 1)], {'line': [1] * 14}, 14)))
        out = self.dir / 'schedule.json'
        out.write_text('old')
        result = self._cli('schedule', '--database', self.db,
                           '--calendar', self.cal, '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(out.read_text(), 'old')
        data['periods'] = 33
        data['demand'] = [{'item': 'P', 'period': 32, 'quantity': 1}]
        source.write_text(json.dumps(data))
        db2 = self.dir / 'long.db'
        self._cli('init', '--source', source, '--database', db2)
        self.cal.write_text(json.dumps(calendar(
            [('P', 'line', 1)], {'line': [1] * 33}, 33)))
        result = self._cli('schedule', '--database', db2,
                           '--calendar', self.cal, '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(out.read_text(), 'old')

    def test_legacy_cli_and_revision_subcommands_unchanged(self):
        out = self.dir / 'legacy.json'
        result = self._cli('--source', FIXTURE, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text())['schema_version'], 1)
        self._init()
        result = self._cli('versions', '--database', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 1)
        result = self._cli('report', '--database', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['revision']['version'], 0)


if __name__ == '__main__':
    unittest.main()
