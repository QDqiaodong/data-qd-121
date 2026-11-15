"""CLI tests for the build/recall traceability subcommands.

The legacy JSON mode and every existing plan subcommand are exercised here as
regression guards as well, so adding the traceability entry point must not
change their arguments, exit codes or outputs.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from material_plan.trace import RecallResult

ROOT = Path(__file__).resolve().parents[1]
PLAN_SOURCE = ROOT / 'fixtures' / 'input.json'
TRACE_CSV = ROOT / 'fixtures' / 'trace_events.csv'


def cli_payload(trace_db, lot):
    """Expected CLI recall envelope: header plus the streamed lot rows."""
    with RecallResult(trace_db, lot) as result:
        return {'header': result.header(),
                'remaining_lots': list(result.rows())}


class TraceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.plan_db = self.dir / 'plan.db'
        self.trace_db = self.dir / 'trace.db'

    def tearDown(self):
        self.temp.cleanup()

    def _cli(self, *arguments):
        return subprocess.run(
            [sys.executable, '-m', 'material_plan.cli', *map(str, arguments)],
            capture_output=True, text=True)

    def _init_plan(self):
        result = self._cli('init', '--source', PLAN_SOURCE,
                           '--database', self.plan_db)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_then_recall_to_stdout_and_file(self):
        self._init_plan()
        result = self._cli('build', '--plan-database', self.plan_db,
                           '--events', TRACE_CSV,
                           '--trace-database', self.trace_db)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['rows'], 10)
        self.assertEqual(summary['lots'], 5)
        self.assertTrue(self.trace_db.exists())

        result = self._cli('recall', '--trace-database', self.trace_db,
                           '--lot', 'a1')
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['header']['total_attributed']['fraction'], '10/1')
        self.assertTrue(report['header']['conservation']['balanced'])
        self.assertEqual(report, cli_payload(self.trace_db, 'a1'))

        out = self.dir / 'recall.json'
        result = self._cli('recall', '--trace-database', self.trace_db,
                           '--lot', 'a1', '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        streamed = json.loads(out.read_text())
        self.assertEqual(streamed, report)
        # The on-disk report is a stream-serialized object with header first.
        self.assertIn('"header"', out.read_text())

    def test_build_failures_exit_2_and_leave_no_database(self):
        self._init_plan()
        bad_csv = self.dir / 'bad.csv'
        bad_csv.write_text(
            'type,event,lot,item,quantity\nreceipt,R1,z9,Z,1\n')
        result = self._cli('build', '--plan-database', self.plan_db,
                           '--events', bad_csv,
                           '--trace-database', self.trace_db)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.trace_db.exists())
        # Re-running build onto an existing non-empty database is also exit 2.
        self._cli('build', '--plan-database', self.plan_db,
                  '--events', TRACE_CSV, '--trace-database', self.trace_db)
        result = self._cli('build', '--plan-database', self.plan_db,
                           '--events', TRACE_CSV,
                           '--trace-database', self.trace_db)
        self.assertEqual(result.returncode, 2)

    def test_recall_bad_lot_exits_2_and_preserves_old_output(self):
        self._init_plan()
        self._cli('build', '--plan-database', self.plan_db,
                  '--events', TRACE_CSV, '--trace-database', self.trace_db)
        out = self.dir / 'keep.json'
        out.write_text('sentinel')
        result = self._cli('recall', '--trace-database', self.trace_db,
                           '--lot', 'P1', '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(out.read_text(), 'sentinel')

    def test_legacy_and_revision_entry_points_unchanged(self):
        # Whole-file legacy mode.
        legacy = self.dir / 'legacy.json'
        result = self._cli('--source', PLAN_SOURCE, '--output', legacy)
        self.assertEqual(result.returncode, 0, result.stderr)
        # Revision store init/apply/report chain.
        self._init_plan()
        batch = self.dir / 'batch.json'
        batch.write_text(json.dumps(
            {'demand': [{'item': 'P', 'period': 3, 'delta': 2}]}))
        result = self._cli('apply', '--database', self.plan_db,
                           '--batch', batch, '--batch-id', 'b1',
                           '--base-version', 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['version'], 1)
        result = self._cli('versions', '--database', self.plan_db)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 2)
        result = self._cli('report', '--database', self.plan_db, '--version', 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['revision']['version'], 0)


if __name__ == '__main__':
    unittest.main()
