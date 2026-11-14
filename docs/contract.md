# Time-phased material planning v1

面向小型装配计划员，将已知期间独立需求、期初库存、已排定到货和BOM转为逐期净需求、
计划到货/下达以及父件需求来源。不是现有批次追溯：这里向前规划供给，不反推已生产物料来源。
periods=1..100，期间编号0..periods-1，无日历换算。items最多500，id唯一，
lead_time=0..100、lot_size=1..10^6整数，on_hand/safety_stock=0..10^9整数单位。
最多2000条BOM边，parent/component已存在、每对唯一、quantity=1..10^6；禁止环和自边。
demand/receipts每类最多20000行item/period/quantity，同item同期间多行相加。

按父件先于子件的确定性拓扑顺序处理；一个共享子件在所有父件需求汇集后再净算。
逐期：net=max(0,gross+safety-opening-scheduled)；planned=net向上取整到lot_size倍数；
closing=opening+scheduled+planned-gross。safety是目标下限，不重复扣耗；超量采购留作后期库存。
父件计划到货t的计划下达期=t-lead_time；组件毛需求在该下达期产生，数量=父件计划量*BOM用量。
不是按父件到货期展开组件。无BOM项视为采购件，但仍使用其自身lead_time/lot策略。

负下达期不夹到0，不隐瞒不可行：past_due_releases和outside_horizon_component_requirements单列。
范围外组件需求不混入期间内净算；计划到货仍是假设可得到的无限产能建议，feasible_within_horizon=false。
不证明产能、供应商、工序或缺料实际可行；不偷偷删需求或调整交期。数量爆炸超过10^18拒绝。
输出逐期账平衡、组件pegging、拓扑顺序及异常。全量重算不累加。

样例P期3需求4，现存1→计划到货3/期2下达；A期2毛需求6，库存3+到货2，安全库存1，
净需2，批量5→期2计划到货5/期1下达、结余4；B期2需求3/到货3。没有past_due。
当前不含日期工作历、产能限制、替代料、损耗率、多版本BOM、订单锁定或持久计划发布。
