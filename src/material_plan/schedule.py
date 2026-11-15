"""Finite-capacity scheduling for one committed plan version.

Each non-zero planned receipt of a non-leaf item becomes one indivisible
assembly job: its quantity and latest completion period (the planned receipt
period) are fixed by the stored plan.  The resource calendar assigns every
assembly item a resource and a 1..8 period duration (the duration replaces
lead_time) and gives every resource a per-period 0/1 capacity.  Leaf items'
planned receipts are fixed purchase supply; on-hand stock and scheduled
receipts are also available.

Within one period, arrivals (scheduled receipts, leaf planned receipts, job
completions) land first, then job starts consume BOM components, then
independent demand is deducted; inventory may never go negative at any step.
A job occupies the contiguous interval [start, start + duration) on its
resource and completes at start + duration, no later than its receipt period.

The backtracking search below jointly respects resource and material
constraints.  It either returns the lexicographically smallest start vector
(jobs ordered by latest completion period, then item) or proves the instance
infeasible; a greedy failure is never reported as infeasible.
"""
from .common import ident, number, require, rows

MAX_PERIODS = 32
MAX_JOBS = 12
MAX_DURATION = 8
MAX_RESOURCES = 128


def parse_calendar(data, periods, items, bom, job_items):
    """Validate the resource calendar against one stored plan version."""
    require(data, 'jobs resources')
    resources = {}
    for row in rows(data['resources'], MAX_RESOURCES):
        require(row, 'id capacity')
        resource = ident(row['id'])
        if resource in resources:
            raise ValueError('duplicate resource: ' + resource)
        capacity = rows(row['capacity'], MAX_PERIODS)
        if len(capacity) != periods:
            raise ValueError(
                'capacity for ' + resource + ' must cover every period')
        resources[resource] = [number(value, 0, 1) for value in capacity]
    config = {}
    for row in rows(data['jobs'], 500):
        require(row, 'item resource duration')
        item = ident(row['item'])
        if item not in items:
            raise ValueError('unknown calendar item: ' + item)
        if not bom[item]:
            raise ValueError('calendar job for purchased leaf item: ' + item)
        if item in config:
            raise ValueError('duplicate calendar job: ' + item)
        resource = ident(row['resource'])
        if resource not in resources:
            raise ValueError('unknown resource: ' + resource)
        config[item] = {'resource': resource,
                        'duration': number(row['duration'], 1, MAX_DURATION)}
    missing = sorted(job_items - set(config))
    if missing:
        raise ValueError('missing calendar job for: ' + ', '.join(missing))
    return config, resources


def schedule_version(source, calendar):
    """Solve one stored plan version against the calendar; build the result.

    ``source`` carries periods, item policies, BOM, planning order, independent
    demand, scheduled receipts and planned receipts for one committed version.
    Raises ValueError on contract violations; returns a structured result for
    both scheduled and proven-infeasible instances.
    """
    periods = source['periods']
    if periods > MAX_PERIODS:
        raise ValueError(
            f'finite scheduling supports at most {MAX_PERIODS} periods')
    items = source['items']
    bom = source['bom']
    jobs = []
    for item in source['order']:
        if not bom[item]:
            continue
        for period, quantity in enumerate(source['planned_receipts'][item]):
            if quantity:
                jobs.append({'item': item, 'receipt_period': period,
                             'quantity': quantity})
    jobs.sort(key=lambda job: (job['receipt_period'], job['item']))
    if len(jobs) > MAX_JOBS:
        raise ValueError(
            f'finite scheduling supports at most {MAX_JOBS} assembly jobs')
    config, resources = parse_calendar(
        calendar, periods, items, bom, {job['item'] for job in jobs})
    for job in jobs:
        spec = config[job['item']]
        job['resource'] = spec['resource']
        job['duration'] = spec['duration']
    # Leaf planned receipts are fixed purchase supply; non-leaf planned
    # receipts became jobs, so their only production supply is completions.
    leaf_supply = {item: ([0] * periods if bom[item]
                          else list(source['planned_receipts'][item]))
                   for item in items}
    starts = _solve(jobs, resources, periods, bom, items,
                    source['demand'], source['receipts'], leaf_supply)
    if starts is None:
        return {'status': 'infeasible',
                'jobs': [_job_row(job, None) for job in jobs],
                'resource_usage': _usage(resources, jobs, None, periods),
                'inventory': []}
    return {'status': 'scheduled',
            'jobs': [_job_row(job, start) for job, start in zip(jobs, starts)],
            'resource_usage': _usage(resources, jobs, starts, periods),
            'inventory': _inventory(source, jobs, starts, leaf_supply)}


def _job_row(job, start):
    return {'item': job['item'], 'receipt_period': job['receipt_period'],
            'quantity': job['quantity'], 'resource': job['resource'],
            'duration': job['duration'], 'start': start,
            'completion': None if start is None else start + job['duration']}


def _solve(jobs, resources, periods, bom, items, demand, receipts, leaf_supply):
    """Complete backtracking search; return starts or None (proven infeasible).

    Jobs are already ordered by (latest completion period, item); trying
    starts in ascending order yields the lexicographically smallest feasible
    start vector.  Pruning is necessary-condition only, so no feasible
    completion of a partial assignment is ever cut:

    - capacity bound: for every horizon T, the slots already occupied on a
      resource plus the durations of unscheduled jobs due by T must fit the
      cumulative 0/1 capacity;
    - optimistic material bound: unscheduled jobs are assumed to complete as
      early as possible (supply) and to consume as late as possible (their
      latest feasible start); if inventory still goes negative, no completion
      of the partial assignment can be feasible.
    """
    count = len(jobs)
    starts = [None] * count
    used = {name: [0] * periods for name in resources}
    cap_prefix = {}
    for name, capacity in resources.items():
        prefix = [0] * (periods + 1)
        for t in range(periods):
            prefix[t + 1] = prefix[t] + capacity[t]
        cap_prefix[name] = prefix
    comp_assigned = {item: [0] * periods for item in items}
    cons_assigned = {item: [0] * periods for item in items}
    producers = {item: [] for item in items}
    consumers = {item: [] for item in items}
    for index, job in enumerate(jobs):
        producers[job['item']].append(index)
        for component, _quantity in bom[job['item']]:
            consumers[component].append(index)

    def material_ok(item):
        balance = items[item]['on_hand']
        completions = comp_assigned[item]
        consumption = cons_assigned[item]
        supply = leaf_supply[item]
        scheduled = receipts[item]
        required = demand[item]
        for t in range(periods):
            balance += (scheduled[t] + supply[t] + completions[t]
                        - consumption[t] - required[t])
            for index in producers[item]:
                job = jobs[index]
                # Earliest possible completion: start at period 0.
                if starts[index] is None and job['duration'] == t:
                    balance += job['quantity']
            for index in consumers[item]:
                job = jobs[index]
                # Latest possible start: the job must have consumed by t.
                if starts[index] is None and job['receipt_period'] - job['duration'] == t:
                    for component, quantity in bom[job['item']]:
                        if component == item:
                            balance -= job['quantity'] * quantity
            if balance < 0:
                return False
        return True

    def capacity_ok(resource):
        prefix = cap_prefix[resource]
        occupancy = used[resource]
        assigned = 0
        for horizon in range(1, periods + 1):
            assigned += occupancy[horizon - 1]
            needed = assigned
            for index, job in enumerate(jobs):
                if (starts[index] is None and job['resource'] == resource
                        and job['receipt_period'] <= horizon):
                    needed += job['duration']
            if needed > prefix[horizon]:
                return False
        return True

    def dfs(k):
        if k == count:
            return True
        job = jobs[k]
        resource = job['resource']
        duration = job['duration']
        due = job['receipt_period']
        affected = [job['item']] + [c for c, _q in bom[job['item']]]
        capacity = resources[resource]
        occupancy = used[resource]
        for s in range(due - duration + 1):
            if any(occupancy[p] >= capacity[p] for p in range(s, s + duration)):
                continue
            starts[k] = s
            for p in range(s, s + duration):
                occupancy[p] += 1
            comp_assigned[job['item']][s + duration] += job['quantity']
            for c, q in bom[job['item']]:
                cons_assigned[c][s] += job['quantity'] * q
            if capacity_ok(resource) and all(material_ok(i) for i in affected):
                if dfs(k + 1):
                    return True
            starts[k] = None
            for p in range(s, s + duration):
                occupancy[p] -= 1
            comp_assigned[job['item']][s + duration] -= job['quantity']
            for c, q in bom[job['item']]:
                cons_assigned[c][s] -= job['quantity'] * q
        return False

    for resource in resources:
        if not capacity_ok(resource):
            return None
    for item in items:
        if not material_ok(item):
            return None
    return starts if dfs(0) else None


def _inventory(source, jobs, starts, leaf_supply):
    """Exact per-period ledger: arrivals, then job starts, then demand."""
    periods = source['periods']
    items = source['items']
    bom = source['bom']
    completions = {item: [0] * periods for item in items}
    consumption = {item: [0] * periods for item in items}
    for job, start in zip(jobs, starts):
        completions[job['item']][start + job['duration']] += job['quantity']
        for component, quantity in bom[job['item']]:
            consumption[component][start] += job['quantity'] * quantity
    ledger = []
    for item in source['order']:
        balance = items[item]['on_hand']
        rows_out = []
        for t in range(periods):
            opening = balance
            after_arrivals = (opening + source['receipts'][item][t]
                              + leaf_supply[item][t] + completions[item][t])
            after_starts = after_arrivals - consumption[item][t]
            closing = after_starts - source['demand'][item][t]
            if after_starts < 0 or closing < 0:
                raise ValueError('internal error: infeasible schedule emitted')
            rows_out.append({
                'period': t,
                'opening_available': opening,
                'scheduled_receipt': source['receipts'][item][t],
                'planned_receipt': leaf_supply[item][t],
                'completions': completions[item][t],
                'job_consumption': consumption[item][t],
                'independent_demand': source['demand'][item][t],
                'projected_available': closing})
            balance = closing
        ledger.append({'item': item, 'periods': rows_out})
    return ledger


def _usage(resources, jobs, starts, periods):
    used = {name: [0] * periods for name in resources}
    if starts is not None:
        for job, start in zip(jobs, starts):
            for p in range(start, start + job['duration']):
                used[job['resource']][p] += 1
    return [{'resource': name,
             'periods': [{'period': t, 'capacity': resources[name][t],
                          'used': used[name][t]}
                         for t in range(periods)]}
            for name in sorted(resources)]
