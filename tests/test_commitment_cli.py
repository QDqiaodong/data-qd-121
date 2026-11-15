"""CLI tests for the leaf commitment subcommands: reserve / release / availability."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from material_plan.store import availability, load_report

# A dedicated, demand-free database: leaf A with 3 on hand and a receipt of 2
# at t2, leaf B with a receipt of 3 at t2, and one non-leaf parent so the leaf
# classification has something to exclude.
PLAN_DATA = {
    'periods': 4,
    'items': [
        {'id': 'P', 'lead_time': 1, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0},
        {'id': 'A', 'lead_time': 1, 'lot_size': 1, 'on_hand': 3, 'safety_stock': 0},
        {'id': 'B', 'lead_time': 0, 'lot_size': 1, 'on_hand': 0, 'safety_stock': 0}],
    'bom': [{'parent': 'P', 'component': 'A', 'quantity': 1}],
    'demand': [],
    'receipts': [
        {'item': 'A', 'period': 2, 'quantity': 2},
        {'item': 'B', 'period': 2, 'quantity': 3}]}

# Five units across two lines, all reachable by period 3 (A supply totals five).
ORDER = {
    'order_id': 1,
    'lines': [
        {'line_no': 0, 'candidates': ['A', 'B'], 'quantity': 4, 'due_period': 3},
        {'line_no': 1, 'candidates': ['A'], 'quantity': 1, 'due_period': 3}]}


class CommitmentCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.db = self.dir / 'plan.db'

    def tearDown(self):
        self.temp.cleanup()

    def _cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'material_plan.cli', *map(str, arguments)],
            capture_output=True, text=True)

    def _write(self, name, payload):
        path = self.dir / name
        path.write_text(json.dumps(payload))
        return path

    def _init_plan_db(self):
        source = self._write('source.json', PLAN_DATA)
        result = self._cli('init', '--source', source, '--database', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reserve_release_availability_lifecycle(self):
        self._init_plan_db()
        order_file = self._write('order.json', ORDER)

        result = self._cli('reserve', '--database', self.db,
                           '--order', order_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['order_id'], 1)
        self.assertFalse(payload['replayed'])
        # Five committed units across the two lines.
        self.assertEqual(sum(r['quantity'] for r in payload['allocations']), 5)

        # Idempotent replay exits 0 and reports replayed=true.
        result = self._cli('reserve', '--database', self.db,
                           '--order', order_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['replayed'])

        # Availability reports per-supply remaining and conservation holds.
        result = self._cli('availability', '--database', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['active_order_ids'], [1])
        for row in report['supply']:
            self.assertEqual(
                row['quantity'],
                row['version_requirement_consumed'] + row['committed']
                + row['remaining'])

        # Availability can also be atomically published to a file.
        out = self.dir / 'avail.json'
        result = self._cli('availability', '--database', self.db,
                           '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text()), report)

        # Release exits 0 and re-allocates the remaining (no) orders.
        result = self._cli('release', '--database', self.db, '--order-id', 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['order_id'], 1)
        self.assertEqual(
            json.loads(self._cli('availability', '--database', self.db).stdout)
            ['active_order_ids'], [])

        # Reusing the released id is a conflict: exit code 3.
        result = self._cli('reserve', '--database', self.db,
                           '--order', order_file)
        self.assertEqual(result.returncode, 3)
        self.assertIn('released', result.stderr)

    def test_infeasible_reservation_exits_three_and_changes_nothing(self):
        self._init_plan_db()
        # A offers 3 on hand + 2 receipt = 5 units; ask for six A-only units.
        impossible = self._write('big.json', {
            'order_id': 7,
            'lines': [{'line_no': 0, 'candidates': ['A'],
                       'quantity': 6, 'due_period': 3}]})
        result = self._cli('reserve', '--database', self.db,
                           '--order', impossible)
        self.assertEqual(result.returncode, 3)
        # No order row and no allocation survived the refused reservation.
        report = json.loads(
            self._cli('availability', '--database', self.db).stdout)
        self.assertEqual(report['active_order_ids'], [])
        self.assertTrue(all(row['committed'] == 0 for row in report['supply']))

    def test_malformed_order_exits_two(self):
        self._init_plan_db()
        bad = self._write('bad.json', {'order_id': 'nope', 'lines': []})
        result = self._cli('reserve', '--database', self.db, '--order', bad)
        self.assertEqual(result.returncode, 2)
        unknown_release = self._cli('release', '--database', self.db,
                                    '--order-id', 999)
        self.assertEqual(unknown_release.returncode, 2)

    def test_legacy_report_still_available_alongside_commitments(self):
        self._init_plan_db()
        order_file = self._write('order.json', ORDER)
        self._cli('reserve', '--database', self.db, '--order', order_file)
        # The plan report contract is untouched by commitment writes.
        result = self._cli('report', '--database', self.db)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn('result', report)
        self.assertIn('plans', report['result'])
        self.assertEqual(report, load_report(self.db))
        # And the Python-level lifecycle agrees with the CLI-created state.
        self.assertEqual(availability(self.db)['active_order_ids'], [1])


if __name__ == '__main__':
    unittest.main()
