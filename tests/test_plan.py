import json,unittest
from pathlib import Path
from material_plan.model import analyze
ROOT=Path(__file__).resolve().parents[1]
class PlanTests(unittest.TestCase):
    def setUp(self):self.d=json.loads((ROOT/'fixtures/input.json').read_text())
    def test_known_netting_and_lead_time(self):
        r=analyze(self.d);p={x['item']:x for x in r['plans']}
        self.assertEqual(p['P']['planned_releases'],[{'period':2,'quantity':3}])
        self.assertEqual(p['A']['periods'][2]['planned_receipt'],5);self.assertEqual(p['A']['periods'][2]['projected_available'],4)
        self.assertTrue(r['feasible_within_horizon'])
    def test_shared_component_netting_occurs_after_all_parents(self):
        self.d['items'].append({'id':'Q','lead_time':1,'lot_size':1,'on_hand':0,'safety_stock':0})
        self.d['bom'].append({'parent':'Q','component':'A','quantity':1})
        self.d['demand'].append({'item':'Q','period':3,'quantity':2})
        a=next(x for x in analyze(self.d)['plans'] if x['item']=='A')['periods'][2]
        self.assertEqual(a['gross_requirement'],8);self.assertEqual(a['planned_receipt'],5)
    def test_past_due_requirements_not_clipped_into_period_zero(self):
        self.d['demand'][0]['period']=0;r=analyze(self.d)
        self.assertFalse(r['feasible_within_horizon']);self.assertEqual(r['past_due_releases'][0]['release_period'],-1)
        self.assertTrue(r['outside_horizon_component_requirements'])
        a=next(x for x in r['plans'] if x['item']=='A');self.assertEqual(a['periods'][0]['gross_requirement'],0)
    def test_period_balance_and_safety(self):
        for p in analyze(self.d)['plans']:
            for x in p['periods']:
                self.assertEqual(x['projected_available'],x['opening_available']+x['scheduled_receipt']+x['planned_receipt']-x['gross_requirement'])
                self.assertGreaterEqual(x['projected_available'],0)
    def test_cycle_and_unknown_item_rejected(self):
        self.d['bom'].append({'parent':'A','component':'P','quantity':1})
        with self.assertRaisesRegex(ValueError,'cycle'):analyze(self.d)
        self.setUp();self.d['receipts'][0]['item']='missing'
        with self.assertRaises(ValueError):analyze(self.d)
    def test_order_invariant_and_repeated_demand_adds(self):
        r=analyze(self.d);self.d['items'].reverse();self.d['bom'].reverse();self.assertEqual(r,analyze(self.d))
        self.d['demand']=[{'item':'P','period':3,'quantity':2}]*2
        self.assertEqual(r,analyze(self.d))
    def test_invalid_boolean_and_zero_lot(self):
        for v in (0,True):
            self.d['items'][0]['lot_size']=v
            with self.assertRaises(ValueError):analyze(self.d)
if __name__=='__main__':unittest.main()
