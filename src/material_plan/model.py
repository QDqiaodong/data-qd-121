from .contracts import parse

def analyze(data):
    periods,items,bom,gross,scheduled,order=parse(data);plans=[];pegging=[];past_due=[]
    # All parents are processed before a shared child, so child gross demand is aggregated before netting.
    for item in order:
        policy=items[item];available=policy['on_hand'];series=[];release_by_period={}
        for t in range(periods):
            opening=available;needed=max(0,gross[item][t]+policy['safety_stock']-opening-scheduled[item][t])
            lot=policy['lot_size'];planned=((needed+lot-1)//lot)*lot
            available=opening+scheduled[item][t]+planned-gross[item][t]
            if max(available,planned,gross[item][t])>10**18:raise ValueError('exploded quantity exceeds 10^18 units')
            release=t-policy['lead_time']
            if planned:
                release_by_period[release]=release_by_period.get(release,0)+planned
                if release<0:past_due.append({'item':item,'release_period':release,'receipt_period':t,'quantity':planned})
                for component,multiplier in sorted(bom[item]):
                    amount=planned*multiplier
                    pegging.append({'parent':item,'component':component,'parent_receipt_period':t,'requirement_period':release,'quantity':amount})
                    if release>=0:gross[component][release]+=amount
            series.append({'period':t,'opening_available':opening,'gross_requirement':gross[item][t],
                           'scheduled_receipt':scheduled[item][t],'net_requirement':needed,
                           'planned_receipt':planned,'projected_available':available})
        plans.append({'item':item,'periods':series,'planned_releases':[{'period':t,'quantity':q} for t,q in sorted(release_by_period.items())]})
    outside=[p for p in pegging if p['requirement_period']<0]
    return {'planning_order':order,'plans':plans,'component_pegging':pegging,'past_due_releases':past_due,
            'outside_horizon_component_requirements':outside,'feasible_within_horizon':not past_due,
            'supply_assumption':'Planned receipts are assumed available even if their release is flagged past due.'}
