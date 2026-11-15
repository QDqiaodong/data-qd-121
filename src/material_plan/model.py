"""Deterministic time-phased netting: full analysis and incremental revision.

The full analysis keeps the original whole-BOM behaviour.  ``revise_plan`` replans
only directly modified items and descendants whose parent releases actually
change; propagation stops at the first item whose plan is unchanged, so a batch
never performs a full analysis.

The leaf commitment allocator (``allocate_commitments``) sits on top of the
same netting kernel: version gross requirements of leaf items are served first
by period FIFO, and the remaining supply is allocated jointly across every
active sales order through a feasibility-oracle search whose test is a
bipartite max-flow, so an order is never granted by greedy first-come logic.
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


# ---------------------------------------------------------------------------
# Leaf-item sales commitment allocation
#
# Supply for commitments is restricted to opening stock (period 0 on-hand) and
# scheduled receipts.  Version gross requirements of leaf items are served
# first, period by period, in FIFO order; unmet gross carries forward as an
# outstanding balance that consumes later arrivals before any commitment does.
# Only supply left after that step is available to active sales orders, and a
# line may consume supply of one of its candidate materials whose supply
# period is no later than the line's due date.
#
# Allocation is one joint decision across every active order (never per-order
# greedy).  Variables are the (line, eligible supply point) pairs in line order
# (order_id, line_no) and, within a line, the global point order
# (item, period, kind).  Each variable is fixed to the largest amount that
# still admits a feasible completion; the feasibility question is answered by
# a bipartite max-flow oracle and the maximum is found by binary search, so
# the constructed allocation is the lexicographically greatest feasible vector
# under the fixed variable ordering.
# ---------------------------------------------------------------------------

# (item, period, supply kind) ordering; 0 = opening stock sorts before the
# scheduled receipt at the same period, so the deterministic tie rule and the
# lexicographic vector ordering agree.
_ON_HAND_KIND = 0
_RECEIPT_KIND = 1


class CommitmentInfeasible(ValueError):
    """The active commitments cannot all be honoured from the remaining supply."""


def leaf_items(order, bom):
    """Items without BOM children, in deterministic planning order."""
    return [item for item in order if not bom[item]]


def build_supply_points(periods, items, receipts, leaves):
    """Ordered supply points: period-0 on-hand first, then scheduled receipts.

    Each point is ``(item, period, kind, quantity)`` where kind 0 is opening
    stock and kind 1 is a scheduled receipt.  Points are sorted by
    (item, period, kind), which is the supply-axis ordering used inside every
    allocation line and by the lexicographic vector ordering.
    """
    points = []
    for item in leaves:
        on_hand = items[item]['on_hand']
        if on_hand:
            points.append((item, 0, _ON_HAND_KIND, on_hand))
        for t in range(periods):
            quantity = receipts[item][t]
            if quantity:
                points.append((item, t, _RECEIPT_KIND, quantity))
    points.sort(key=lambda point: (point[0], point[1], point[2]))
    return points


def leaf_gross_requirements(periods, items, bom, demand, receipts, order, leaves):
    """Leaf-item gross requirements of the current plan version.

    Independent demand plus parent-release contributions, taken directly from a
    full netting run so commitments and the stored plan always share one kernel.
    """
    result = run_full(periods, items, bom, demand, receipts, order)
    gross = {item: [0] * periods for item in leaves}
    for plan in result['plans']:
        if plan['item'] in gross:
            for row in plan['periods']:
                gross[plan['item']][row['period']] = row['gross_requirement']
    return gross


def fifo_version_consumption(points, periods, leaves, gross):
    """FIFO netting of version gross requirements against the supply points.

    Gross is served period by period per leaf item; unmet quantity carries
    forward and consumes the earliest later arrivals, so a commitment can never
    take supply that an outstanding version requirement needs.  Returns the
    residual quantity of every supply point and the per-item carried shortfall
    (zero when the version plan is fully covered).
    """
    residual = {point[:3]: point[3] for point in points}
    # Supply points per item, already in (period, kind) order: earliest first.
    by_item = {item: [point for point in points if point[0] == item]
               for item in leaves}
    shortfall = {item: [0] * periods for item in leaves}
    for item in leaves:
        cursor = 0
        item_points = by_item[item]
        outstanding = 0
        for t in range(periods):
            outstanding += gross[item][t]
            # Serve the outstanding balance from the earliest not-yet-used
            # supply point whose period is at least t.  Points of earlier
            # periods were fully consumed when the cursor passed them; points
            # of later periods simply wait here for a future period's loop,
            # and the carried outstanding balance reaches them then.
            while outstanding and cursor < len(item_points):
                point = item_points[cursor]
                if point[1] > t:
                    # Earliest remaining supply arrives after this period; it
                    # stays reserved for a later loop, the balance carries.
                    break
                key = point[:3]
                take = min(outstanding, residual[key])
                residual[key] -= take
                outstanding -= take
                if residual[key] == 0:
                    cursor += 1
            shortfall[item][t] = outstanding
    return residual, shortfall


def _max_flow_feasible(lines, points, residual, forced=None):
    """Bipartite b-matching feasibility for the residual line requirements.

    Lines need their still-open quantity split across eligible supply points;
    a point can serve several lines up to its residual quantity.  ``forced``
    optionally maps a supply-point key to a quantity already committed by
    earlier (fixed) variables, which is subtracted from the point's capacity.
    Dinic on this bipartite network returns whether every line demand can be
    met, which is a necessary-and-sufficient condition for any completion of
    the partial allocation -- greedy failures never decide infeasibility.
    """
    forced = forced or {}
    open_demand = [line['_remaining'] for line in lines]
    if not any(open_demand):
        return True

    def point_residual(idx):
        key = points[idx][:3]
        return residual[key] - forced.get(key, 0)

    active_points = {idx for line in lines for idx in line['_eligible']
                     if point_residual(idx) > 0}
    if not active_points:
        return False
    point_nodes = {idx: len(lines) + k for k, idx in enumerate(sorted(active_points))}
    source = len(lines) + len(point_nodes)
    sink = source + 1
    graph = [[] for _ in range(sink + 1)]

    def add_edge(u, v, capacity):
        graph[u].append([v, capacity, len(graph[v])])
        graph[v].append([u, 0, len(graph[u]) - 1])

    for i, need in enumerate(open_demand):
        if need:
            add_edge(source, i, need)
    for idx, node in point_nodes.items():
        add_edge(node, sink, point_residual(idx))
    for i, line in enumerate(lines):
        if not open_demand[i]:
            continue
        for idx in line['_eligible']:
            node = point_nodes.get(idx)
            if node is not None:
                add_edge(i, node, open_demand[i])

    total = sum(open_demand)

    def augment():
        """One BFS level graph, then iterative blocking-flow augmentation.

        The parent chain records (node, edge index); reverse edges only lead
        to already-visited lower levels, so an explicit queue/path never loops
        the way a recursive push over paired edges can.
        """
        level = [-1] * (sink + 1)
        level[source] = 0
        queue = [source]
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            for edge in graph[u]:
                if edge[1] and level[edge[0]] < 0:
                    level[edge[0]] = level[u] + 1
                    queue.append(edge[0])
        if level[sink] < 0:
            return 0
        moved_total = 0
        # Next edge to inspect per node (current-arc optimization).
        pointer = [0] * (sink + 1)
        while moved_total < total:
            parent_node = [-1] * (sink + 1)
            parent_edge = [-1] * (sink + 1)
            parent_node[source] = source
            stack = [source]
            found = False
            while stack and not found:
                u = stack[-1]
                if u == sink:
                    found = True
                    break
                advanced = False
                while pointer[u] < len(graph[u]):
                    edge_index = pointer[u]
                    edge = graph[u][edge_index]
                    if edge[1] and level[edge[0]] == level[u] + 1 \
                            and parent_node[edge[0]] < 0:
                        parent_node[edge[0]] = u
                        parent_edge[edge[0]] = edge_index
                        stack.append(edge[0])
                        advanced = True
                        break
                    pointer[u] += 1
                if not advanced:
                    stack.pop()
                    level[u] = -1
            if not found:
                break
            bottleneck = total - moved_total
            node = sink
            while node != source:
                edge = graph[parent_node[node]][parent_edge[node]]
                bottleneck = min(bottleneck, edge[1])
                node = parent_node[node]
            node = sink
            while node != source:
                u = parent_node[node]
                edge_index = parent_edge[node]
                edge = graph[u][edge_index]
                edge[1] -= bottleneck
                graph[node][edge[2]][1] += bottleneck
                node = u
            moved_total += bottleneck
        return moved_total

    flow = 0
    while flow < total:
        moved = augment()
        if not moved:
            break
        flow += moved
    return flow == total


def allocate_commitments(periods, items, bom, demand, receipts, order, orders):
    """Allocate remaining leaf supply jointly across all active sales orders.

    ``orders`` is a list of validated order dicts, each with integer
    ``order_id`` and ``lines``; every line has ``line_no``, sorted unique
    ``candidates`` (leaf item ids), integer ``quantity`` and ``due_period``.
    Returns a dict with the ordered supply points, per-point residual after the
    version FIFO step (and after commitments), the carried version shortfall
    and the chosen allocation.  Raises :class:`CommitmentInfeasible` when no
    joint allocation satisfies every active order.
    """
    leaves = set(leaf_items(order, bom))
    points = build_supply_points(periods, items, receipts, leaves)
    gross = leaf_gross_requirements(
        periods, items, bom, demand, receipts, order, leaves)
    residual, shortfall = fifo_version_consumption(
        points, periods, leaves, gross)
    residual_after_version = dict(residual)

    lines = []
    for order_entry in sorted(orders, key=lambda entry: entry['order_id']):
        for line in sorted(order_entry['lines'], key=lambda entry: entry['line_no']):
            due = line['due_period']
            eligible = [idx for idx, (item, period, _kind, _q) in enumerate(points)
                        if item in line['candidates'] and period <= due]
            lines.append({
                'order_id': order_entry['order_id'],
                'line_no': line['line_no'],
                'candidates': line['candidates'],
                'due_period': due,
                'quantity': line['quantity'],
                '_eligible': eligible,
                '_remaining': line['quantity']})
    if not lines:
        return {'points': points,
                'residual_after_version': residual_after_version,
                'residual': dict(residual), 'shortfall': shortfall,
                'allocations': [], 'gross': gross}

    allocation = []
    # Quantities already fixed on each supply point by earlier variables.
    forced = {}

    # Construct the lexicographically greatest allocation one variable at a
    # time.  Variables are the (line, eligible supply point) pairs in line
    # order ((order_id, line_no)) then global point order ((item, period,
    # kind)).  For each variable we fix the largest amount that still admits a
    # feasible completion of every not-yet-finished line.  "A completion with
    # this edge carrying at least x exists" is monotone in x, so the maximum
    # is found by binary search and each feasibility question is answered
    # exactly by the bipartite max-flow test -- never by a greedy guess.
    for k, line in enumerate(lines):
        need = line['quantity']
        picks = [idx for idx in line['_eligible']
                 if residual[points[idx][:3]] - forced.get(points[idx][:3], 0) > 0]
        for position, idx in enumerate(picks):
            key = points[idx][:3]
            free = residual[key] - forced.get(key, 0)
            later_capacity = sum(
                residual[points[other][:3]] - forced.get(points[other][:3], 0)
                for other in picks[position + 1:])
            # Below this bound the line's remaining quantity cannot fit into
            # the points that follow this one.
            lower = max(0, need - later_capacity)
            upper = min(need, free)

            def feasible_at(amount):
                forced[key] = forced.get(key, 0) + amount
                line['_remaining'] = need - amount
                ok = _max_flow_feasible(lines, points, residual, forced)
                forced[key] -= amount
                return ok

            if lower == upper:
                # No choice of value on this variable.
                if not feasible_at(lower):
                    raise CommitmentInfeasible(
                        'active sales commitments cannot be jointly satisfied')
                best = lower
            else:
                best = None
                lo, hi = lower, upper
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if feasible_at(mid):
                        best = mid
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if best is None:
                    raise CommitmentInfeasible(
                        'active sales commitments cannot be jointly satisfied')
            forced[key] = forced.get(key, 0) + best
            need -= best
            # Keep the oracle-visible remaining demand consistent with the
            # fixed pick regardless of which probe ran last.
            line['_remaining'] = need
            if best:
                item, period, kind = key
                allocation.append({
                    'order_id': line['order_id'],
                    'line_no': line['line_no'],
                    'item': item,
                    'supply_period': period,
                    'supply_kind': 'on_hand' if kind == _ON_HAND_KIND
                    else 'scheduled_receipt',
                    'quantity': best})
            if need == 0:
                break
        if need:
            raise CommitmentInfeasible(
                'active sales commitments cannot be jointly satisfied')
        line['_remaining'] = 0

    # Final residual per point after both the version FIFO step and commitments.
    final_residual = {key: residual[key] - forced.get(key, 0) for key in residual}
    allocation.sort(key=lambda row: (row['order_id'], row['line_no'],
                                     row['item'], row['supply_period'],
                                     row['supply_kind']))
    return {'points': points,
            'residual_after_version': residual_after_version,
            'residual': final_residual, 'shortfall': shortfall,
            'allocations': allocation, 'gross': gross}
