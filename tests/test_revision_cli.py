"""CLI tests for the SQLite revision subcommands; legacy JSON mode is unchanged."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from material_plan.cli import run
from material_plan.store import load_report

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'fixtures' / 'input.json'


class RevisionCliTests(unittest.TestCase):
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

    def test_init_apply_report_version_chain(self):
        result = self._cli('init', '--source', SOURCE,
                           '--database', self.db, '--output', self.dir / 'v0.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {'database': str(self.db), 'version': 0, 'batch_id': None})
        v0 = json.loads((self.dir / 'v0.json').read_text())
        self.assertEqual(v0['result'], run(SOURCE)['result'])

        batch = self.dir / 'batch.json'
        batch.write_text(json.dumps(
            {'demand': [{'item': 'P', 'period': 3, 'delta': 2}]}))
        result = self._cli('apply', '--database', self.db, '--batch', batch,
                           '--batch-id', 'b1', '--base-version', 0,
                           '--output', self.dir / 'v1.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['version'], 1)

        # Identical replay returns the same version with replayed=true.
        result = self._cli('apply', '--database', self.db, '--batch', batch,
                           '--batch-id', 'b1', '--base-version', 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['version'], 1)
        self.assertTrue(payload['replayed'])

        # Stale base and duplicate id with different content exit 3.
        result = self._cli('apply', '--database', self.db, '--batch', batch,
                           '--batch-id', 'b2', '--base-version', 0)
        self.assertEqual(result.returncode, 3)
        other = self.dir / 'other.json'
        other.write_text(json.dumps(
            {'demand': [{'item': 'P', 'period': 3, 'delta': 3}]}))
        result = self._cli('apply', '--database', self.db, '--batch', other,
                           '--batch-id', 'b1', '--base-version', 1)
        self.assertEqual(result.returncode, 3)

        # Historical version 0 remains readable after version 1 exists.
        result = self._cli('report', '--database', self.db, '--version', 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['revision']['version'], 0)
        result = self._cli('report', '--database', self.db)
        head_report = json.loads(result.stdout)
        self.assertEqual(head_report['revision']['version'], 1)
        self.assertEqual(head_report, load_report(self.db))

    def test_report_to_file_uses_atomic_publication(self):
        self._cli('init', '--source', SOURCE, '--database', self.db)
        out = self.dir / 'report.json'
        result = self._cli('report', '--database', self.db, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text()), load_report(self.db))

    def test_legacy_json_mode_keeps_original_contract(self):
        out = self.dir / 'legacy.json'
        result = self._cli('--source', SOURCE, '--output', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text()), run(SOURCE))
        # Legacy failures still exit 2 and preserve the previous report.
        bad = self.dir / 'bad.json'
        bad.write_text('{}')
        result = self._cli('--source', bad, '--output', out)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(out.read_text()), run(SOURCE))


if __name__ == '__main__':
    unittest.main()
