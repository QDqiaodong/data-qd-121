"""Property-style checks: random batches always match full netting.

Across randomized DAGs (chains, diamonds, shared components, unrelated items)
and random signed deltas, every stored revision is reconstructed from its
historical inputs and fully re-netted with ``analyze``.  The incremental
result must match exactly, and ``recomputed_items`` must be a subset of the
modified items plus their BOM descendants -- never an unrelated item, and
never a full-BOM analysis when a single item changed without propagation.
"""
import json
import random
import tempfile
import unittest
from pathlib import Path

from material_plan.model import analyze
from material_plan.store import (RevisionError, apply_batch, initialize,
                                 load_report)
from tests.test_revision import full_data_from_store, full_data_from_store_at


def descendants(bom, roots):
    seen = set()
    stack = list(roots)
    while stack:
        item = stack.pop()
        for child, _q in bom.get(item, ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


class RandomizedEquivalenceTests(unittest.TestCase):
    def test_random_batches_never_diverge_from_full_netting(self):
        rng = random.Random(20261114)
        for trial in range(12):
            with self.subTest(trial=trial):
                self._run_trial(rng)

    def _run_trial(self, rng):
        periods = rng.randint(3, 8)
        names = [f'I{k}' for k in range(rng.randint(4, 9))]
        items = [{
            'id': name,
            'lead_time': rng.randint(0, 3),
            'lot_size': rng.choice([1, 2, 5, 10]),
            'on_hand': rng.choice([0, 0, 1, 5, 20]),
            'safety_stock': rng.choice([0, 0, 1])} for name in names]
        # Random acyclic edges only from earlier items to later items.
        bom = []
        for i, parent in enumerate(names):
            for child in names[i + 1:]:
                if rng.random() < 0.35:
                    bom.append({'parent': parent, 'component': child,
                                'quantity': rng.randint(1, 3)})
        bom_map = {}
        for edge in bom:
            bom_map.setdefault(edge['parent'], []).append(
                (edge['component'], edge['quantity']))
        demand = [{'item': rng.choice(names), 'period': rng.randrange(periods),
                   'quantity': rng.randint(1, 12)} for _ in range(rng.randint(1, 6))]
        data = {'periods': periods, 'items': items, 'bom': bom,
                'demand': demand, 'receipts': []}

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / 'random.db'
            initialize(db_path, data, '0' * 64)
            self.assertEqual(load_report(db_path, 0)['result'], analyze(data))

            version = 0
            rejected = 0
            for step in range(18):
                kind = rng.choice(['demand', 'demand', 'receipts'])
                item = rng.choice(names)
                period = rng.randrange(periods)
                delta = rng.choice([-3, -2, -1, 1, 2, 3, 5])
                payload = {kind: [{'item': item, 'period': period, 'delta': delta}]}
                try:
                    new_version, replayed = apply_batch(
                        db_path, f'b{step}', version, payload)
                except RevisionError:
                    rejected += 1
                    continue
                self.assertFalse(replayed)
                version = new_version
                report = load_report(db_path, version)
                # Exact match against a full, independent recomputation of the
                # inputs as they existed at this version.
                self.assertEqual(
                    report['result'],
                    analyze(full_data_from_store_at(db_path, version)))
                # Recomputed items must stay within the modified subtree.
                allowed = {item} | descendants(bom_map, {item})
                self.assertTrue(set(report['recomputed_items']) <= allowed,
                                (report['recomputed_items'], allowed))
                # The directly modified item is always replanned.
                self.assertIn(item, report['recomputed_items'])
            # Head and every historical version remain independently verifiable.
            for old_version in range(version + 1):
                report = load_report(db_path, old_version)
                self.assertEqual(
                    report['result'],
                    analyze(full_data_from_store_at(db_path, old_version)))
            self.assertEqual(
                load_report(db_path)['result'],
                analyze(full_data_from_store(db_path)))
            self.assertGreater(version, 0)


if __name__ == '__main__':
    unittest.main()
