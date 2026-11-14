from .common import require,ident,number,rows

def parse(data):
    require(data,'periods items bom demand receipts');periods=number(data['periods'],1,100);items={};bom={};demand={};receipts={}
    for row in rows(data['items'],500):
        require(row,'id lead_time lot_size on_hand safety_stock');key=ident(row['id'])
        if key in items:raise ValueError('duplicate item')
        items[key]={'lead_time':number(row['lead_time'],0,100),'lot_size':number(row['lot_size'],1,10**6),
                    'on_hand':number(row['on_hand']), 'safety_stock':number(row['safety_stock'])}
        bom[key]=[];demand[key]=[0]*periods;receipts[key]=[0]*periods
    if not items:raise ValueError('items cannot be empty')
    seen=set()
    for edge in rows(data['bom'],2000):
        require(edge,'parent component quantity');p=ident(edge['parent']);c=ident(edge['component']);q=number(edge['quantity'],1,10**6)
        if p not in items or c not in items or (p,c) in seen or p==c:raise ValueError('invalid or duplicate BOM edge')
        seen.add((p,c));bom[p].append((c,q))
    for key,result in [('demand',demand),('receipts',receipts)]:
        for row in rows(data[key],20000):
            require(row,'item period quantity');item=ident(row['item']);period=number(row['period'],0,periods-1)
            if item not in items:raise ValueError('unknown item requirement')
            result[item][period]+=number(row['quantity'])
    indegree={i:0 for i in items}
    for edges in bom.values():
        for c,q in edges:indegree[c]+=1
    ready=sorted(i for i in items if not indegree[i]);order=[]
    while ready:
        p=ready.pop(0);order.append(p)
        for c,q in sorted(bom[p]):
            indegree[c]-=1
            if indegree[c]==0:ready.append(c);ready.sort()
    if len(order)!=len(items):raise ValueError('BOM cycle')
    return periods,items,bom,demand,receipts,order
