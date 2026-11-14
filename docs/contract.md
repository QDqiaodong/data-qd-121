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

# SQLite 增量修订 v2

在 v1 全量契约之上增加仅追加（append-only）的版本库，标准库 `sqlite3`，无外部服务。
BOM 边与物料策略（lead_time/lot_size/on_hand/safety_stock）在 `init` 后固定，
修订只允许改 demand/receipts；周期数与物料集合同样固定。

## 版本与批次规则

- `init` 把原 JSON 原样导入为版本 0：逐期净算结果、源字节 SHA256、拓扑顺序一并落库；
  已存在的数据库文件拒绝重复初始化，源数据非法时不留下数据库文件。
- `apply` 接收基版本号、批次 ID 与负载：`{"demand": [...], "receipts": [...]}`，
  每行 `{item, period, delta}`，delta 为有符号整数（±10^9 内），period 在 0..99 且不越出当前周期。
  同 item/period/kind 的多行在批次内先相加合并；合并后的绝对量必须落在 0..10^9，负值拒绝；
  全部 delta 净额为零的批次拒绝建版。
- 同批次 ID 同内容（规范化 JSON 的 SHA256 相同）幂等返回原版本号，标记 `replayed=true`；
  同 ID 异内容拒绝。新批次的基版本必须等于当前头版本，否则以冲突拒绝（退出码 3）。
  并发写入借助 `BEGIN IMMEDIATE` 串行化，竞争批次仅一个成功，其余得到冲突错误。
- 输入单元格、批次、版本、逐物料快照、逐项逐期差额、recomputed_items 与头指针在
  同一事务提交；进程在提交前终止则整笔回滚，重开后无半版，旧版本始终可读。

## 增量重算边界

- 直接修改物料（含非零 demand/receipt 净 delta）首先重算；仅当某物料的计划下达
  （按期间的数量映射）实际变化时，才撤回它在子件上的旧贡献并注入新贡献，然后把子件入队。
- 共享子件在所有父件贡献于拓扑序中汇集后再净算；传播在首个计划未变化的物料处停止，
  任何批次都不做全 BOM analyze。`recomputed_items` 只含直接修改物料及下达变化真正传播到的后代。
- 负下达期沿用 v1 隔离：不夹到 0，不进入子件期内毛需求，只进入 past_due_releases 与
  outside_horizon_component_requirements；数量超 10^18 仍拒绝。
- 每次修订保存：完整逐期计划（仅重算物料写新快照，读取时按物料取最新快照拼全量）、
  逐项逐期有符号差额（series 六字段与 planned_releases 数量）、recomputed_items。

## 报告与读取

`report` 可读取任一已提交版本（默认当前头），输出在 v1 结果之外附加 `revision`、
`plan_diffs`、`recomputed_items`；旧版本输入与计划永久保留。`versions` 列出版本链。
原 JSON 直读直出的 CLI 行为（`--source/--output`、退出码、原子发布、源哈希）保持不变。

