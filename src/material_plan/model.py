"""Deterministic time-phased netting: full analysis and incremental revision.

The full analysis keeps the original whole-BOM behaviour.  ``revise_plan`` replans
only directly modified items and descendants whose parent releases actually
change; propagation stops at the first item whose plan is unchanged, so a batch
never performs a full analysis.
"""
from .contracts import parse

SERIES_FIELDS = ('opening_available', 'gross_requirement', 'scheduled_receipt',
                 'net_requirement', 'planned_receipt', 'projected_available')


def plan_item(item, periods, policy, bom, gross, scheduled):
    """Run the period netting for one item; return series, releases, pegging, past due."""
    series = []
    release_by_period = {}
    pegging = []
    past_due = []
    available = policy['on_hand']
    for t in range(periods):
        opening = available
        needed = max(0, gross[t] + policy['safety_stock'] - opening - scheduled[t])
        lot = policy['lot_size']
        planned = ((needed + lot - 1) // lot) * lot
        available = opening + scheduled[t] + planned - gross[t]
        if max(available, planned, gross[t]) > 10 ** 18:
            raise ValueError('exploded quantity exceeds 10^18 units')
        release = t - policy['lead_time']
        if planned:
            release_by_period[release] = release_by_period.get(release, 0) + planned
            if release < 0:
                past_due.append({'item': item, 'release_period': release,
                                 'receipt_period': t, 'quantity': planned})
            for component, multiplier in sorted(bom[item]):
                amount = planned * multiplier
                pegging.append({'parent': item, 'component': component,
                                'parent_receipt_period': t, 'requirement_period': release,
                                'quantity': amount})
        series.append({'period': t, 'opening_available': opening,
                       'gross_requirement': gross[t], 'scheduled_receipt': scheduled[t],
                       'net_requirement': needed, 'planned_receipt': planned,
                       'projected_available': available})
    releases = [{'period': t, 'quantity': q} for t, q in sorted(release_by_period.items())]
    return series, releases, pegging, past_due


def run_full(periods, items, bom, demand, receipts, order):
    """Whole-BOM analysis; callers own the matrices and may pass references."""
    gross = {item: list(demand[item]) for item in items}
    scheduled = receipts
    plans = []
    pegging = []
    past_due = []
    # All parents are processed before a shared child, so child gross demand is
    # aggregated before netting.
    for item in order:
        series, releases, item_pegging, item_past_due = plan_item(
            item, periods, items[item], bom, gross[item], scheduled[item])
        plans.append({'item': item, 'periods': series, 'planned_releases': releases})
        pegging.extend(item_pegging)
        past_due.extend(item_past_due)
        for peg in item_pegging:
            if peg['requirement_period'] >= 0:
                gross[peg['component']][peg['requirement_period']] += peg['quantity']
    outside = [p for p in pegging if p['requirement_period'] < 0]
    return {'planning_order': order, 'plans': plans, 'component_pegging': pegging,
            'past_due_releases': past_due,
            'outside_horizon_component_requirements': outside,
            'feasible_within_horizon': not past_due,
            'supply_assumption': 'Planned receipts are assumed available even if their release is flagged past due.'}


def analyze(data):
    periods, items, bom, gross, scheduled, order = parse(data)
    return run_full(periods, items, bom, gross, scheduled, order)


def _series_map(series):
    return {row['period']: row for row in series}


def _release_map(releases):
    return {row['period']: row['quantity'] for row in releases}


def revise_plan(periods, items, bom, order, demand, receipts, head_plans,
                demand_delta, receipt_delta):
    """Incrementally revise a stored plan.

    Directly modified items (non-zero demand/receipt deltas) are replanned first.
    An item is enqueued only when its parent's planned releases change; the old
    parent contribution is withdrawn and the new one injected, so shared
    components accumulate every parent's contribution topologically before they
    are netted.  Propagation stops as soon as a replanned item keeps its plan.

    Returns ``(plans, diffs, pegging, past_due, recomputed)`` where ``plans``
    holds full replacement plans only for recomputed items.
    """
    dirty = {item for item in demand_delta if demand_delta[item]}
    dirty.update(item for item in receipt_delta if receipt_delta[item])
    plans = dict(head_plans)
    # Mutable gross and scheduled receipts.  Gross starts from independent
    # demand plus the current parent contributions; scheduled starts from the
    # stored receipts merged with this batch's receipt deltas.  Only dirty items
    # are ever netted, but their children may receive withdrawn/re-injected
    # contributions.
    gross = {item: [demand[item][t] for t in range(periods)] for item in items}
    scheduled = {
        item: [receipts[item][t] + receipt_delta.get(item, {}).get(t, 0)
               for t in range(periods)] for item in items}
    # Independent demand deltas are applied up front so every item's gross is
    # built from the post-batch independent demand plus current parent
    # contributions.
    for item, period_values in demand_delta.items():
        for period, delta in period_values.items():
            gross[item][period] += delta
    for item in items:
        for peg in plans[item]['component_pegging']:
            if peg['requirement_period'] >= 0:
                gross[peg['component']][peg['requirement_period']] += peg['quantity']
    recomputed = []
    diffs = {}
    new_pegging = {}
    new_past_due = {}
    pending = set(dirty)
    # Strict topological mark-and-scan: an item is netted only after every
    # parent's contribution has been withdrawn/re-injected, so a shared
    # component always aggregates its complete new gross before netting.
    for item in order:
        if item not in pending:
            continue
        old = plans[item]
        series, releases, pegging, past_due = plan_item(
            item, periods, items[item], bom, gross[item], scheduled[item])
        new_plan = {'item': item, 'periods': series, 'planned_releases': releases,
                    'component_pegging': pegging, 'past_due_releases': past_due}
        recomputed.append(item)
        diffs[item] = _plan_diff(old, new_plan)
        old_releases = _release_map(old['planned_releases'])
        new_releases = _release_map(new_plan['planned_releases'])
        if new_releases != old_releases:
            # Withdraw the old parent contribution, then inject the new one.
            # Negative release periods stay isolated: they never touch in-horizon
            # child gross and are only recorded as outside-horizon requirements.
            for peg in old['component_pegging']:
                if peg['requirement_period'] >= 0:
                    gross[peg['component']][peg['requirement_period']] -= peg['quantity']
            for peg in pegging:
                if peg['requirement_period'] >= 0:
                    gross[peg['component']][peg['requirement_period']] += peg['quantity']
            for component, _q in bom[item]:
                pending.add(component)
        plans[item] = new_plan
        new_pegging[item] = pegging
        new_past_due[item] = past_due
    return plans, diffs, new_pegging, new_past_due, recomputed


def _plan_diff(old, new):
    """Per-item, per-period signed deltas (only non-zero entries are kept)."""
    diff = {'series': [], 'planned_releases': []}
    old_series = _series_map(old['periods'])
    new_series = _series_map(new['periods'])
    for period in sorted(set(old_series) | set(new_series)):
        before = old_series.get(period, {})
        after = new_series.get(period, {})
        fields = {}
        for field in SERIES_FIELDS:
            delta = after.get(field, 0) - before.get(field, 0)
            if delta:
                fields[field] = delta
        if fields:
            diff['series'].append({'period': period, **fields})
    old_releases = _release_map(old['planned_releases'])
    new_releases = _release_map(new['planned_releases'])
    for period in sorted(set(old_releases) | set(new_releases)):
        delta = new_releases.get(period, 0) - old_releases.get(period, 0)
        if delta:
            diff['planned_releases'].append({'period': period, 'delta': delta})
    return diff
