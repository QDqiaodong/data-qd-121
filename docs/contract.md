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

# 有限产能排程 v3

`schedule` 子命令以**只读**方式打开版本库（`mode=ro`，绝不写入、不建版本、不缺省建文件），
读取指定版本（默认当前头）的计划，叠加一份资源日历 JSON，把非叶物料的计划到货落成
有限产能下的装配作业开工期。限 32 期、12 个装配作业，超出即输入错误。

## 作业与日历

- 非叶物料每个非零 planned_receipt 生成一个**不可拆作业**：数量固定为该计划到货量，
  最迟完工期固定为该到货期；作业可在更早时期完工，提前量进入库存。
- 日历 `{"jobs": [...], "resources": [...]}` 仅这两个字段。`jobs` 每行
  `{item, resource, duration}`：item 必须存在且为非叶（叶件为采购件，拒绝配置），
  resource 必须已定义，duration 为 1..8 整数期，**工时替代 lead_time**；同一物料重复配置拒绝。
  每个有作业的非叶物料必须配置；为尚无作业的非叶物料预留配置不报错但不参与排程。
- `resources` 每行 `{id, capacity}`：capacity 为逐期 0/1 整数数组，长度必须等于周期数；
  0 表示该期停机。资源 id 重复拒绝，未使用资源允许。
- 叶件 planned_receipt 是固定采购供给，期初库存与 scheduled_receipt 同样可用。

## 期间语义

作业占用资源 `[start, start+duration)` 的连续区间，逐期占用量不得超过该期 0/1 容量，
完工期 = start+duration ≤ 最迟完工期。同一期内先到货/完工（scheduled_receipt、叶件
planned_receipt、作业完工入库），再开工（按 BOM 用量扣减组件），再扣独立需求；
每一步库存均不得为负。开工期的组件扣减替代 v1 的提前期展开，排程不再使用 lead_time。

## 求解与输出

回溯搜索联合检查资源与物料约束：容量前缀界（已占用 + 未排作业在必达窗口内的工时
不得超过累计容量）与物料乐观界（未排作业按最早完工计供给、按最迟开工计消耗，仍负则剪枝）
均为必要条件剪枝，不割可行解，因此要么给出解、要么**完备判定无解**——贪心失败不算无解。
多解时按（最迟完工期, item）作业序的开工向量取字典序最小。

报告含 `revision`（读取版本与当前头）、`calendar_sha256` 与 `result`：`status`
（scheduled/infeasible）、`jobs`（item、数量、最迟完工期、资源、工时、开工期、完工期；
无解时开工/完工为 null）、逐资源逐期 `resource_usage`（capacity 与 used）、逐物料逐期
库存账（期初、scheduled_receipt、叶件 planned_receipt、完工入库、作业扣件、独立需求、
期末，逐期平衡）。无解同样输出结构化结果（库存账为空、占用为零），退出码 0；
输入错误退出 2 并保留旧输出文件。修订后仍可用 `--version` 读取任一旧版本排程。

# 实际批次追溯 v4

在版本库（v2）之外新增**独立的追溯库**与 `build`/`recall` 两个 CLI 子命令。
`build` 以只读方式（`mode=ro`）打开计划库，只取物料标识与 BOM，绝不写入计划库；
计划 JSON 直读模式、修订库与排程的既有入口、退出码和输出保持不变。

## 事件 CSV 与导入契约

- 输入为 UTF-8 CSV，表头严格为 `type,event,lot,item,quantity`，最多 200000 数据行、
  文件不超过 64 MiB；按行流式读入，每 1000 行一批写入暂存表，明细与依赖边从不整体
  装入 Python 内存。`quantity` 为严格正十进制整数（1..10^9），标识符规则与计划库一致。
- `type` 仅允许 `receipt`/`consume`/`produce`，行序任意（乱序由库内依赖解析归位）。
- `receipt` 是叶件的**唯一新 lot 来源**：receipt 行只能建叶件 lot；`produce` 行只能建
  非叶件 lot；每个 lot 恰好由一条 receipt/produce 行创建，重复产出拒绝。
- 一个生产 event 可 `consume` 多个旧 lot、`produce` 同一父件的多个新 lot；同一 event 内
  对同一 lot 只能有一条 consume。receipt event 只允许 receipt 行；生产 event 必须同时
  至少有一条 produce 和一条 consume，且只产出一个父件。
- 每个组件在一个生产 event 上的**总耗用量必须恰好等于该 event 父件总产量 × BOM 用量**；
  耗用未定义 lot（悬空引用）、lot 与 item 不符、耗用非该父件 BOM 组件、同一 lot 跨 event
  累计耗用超过其 lot 数量（累计超领）全部拒绝。
- 拒绝条件还包括：未知件（不在计划库物料集）、lot 依赖成环。生产顺序由 lot 依赖经
  SQLite 内的批量 Kahn 扫描确定（lot → 领用 event → 产出 lot），全部为集合式 SQL，
  不把边装入内存；扫不动的残余 event 即证明存在循环。
- 整批失败无半仓：导入在目标目录的隐藏临时库内单事务完成，提交后才 `os.replace`
  原子发布到目标路径；任何校验失败或进程中止都只删除临时库，目标路径不出现数据库，
  已存在的非空目标库拒绝覆盖。成功后落库 event、lot（含生产顺序 seq）、逐 event 的
  领用/产出行、BOM 物料引用以及源文件 SHA256 与行数元数据。

## recall 完全混匀传播契约

- `recall --trace-database <库> --lot <原料 receipt lot>` 只接受 receipt lot；以只读连接
  打开追溯库（临时表除外不写任何持久数据），按生产顺序逐 event 传播该来源 lot 的数量。
- 完全混匀：event 对某个被领用 lot 取其来源占比 `attr(lot) × 领用量 / lot 总量`，
  各被领用 lot 的贡献汇成 event 池；产出 lot 按 `产量 / event 总产量` 分摊该池。
  部分领用与多产出都按比例传播；先分拆再汇合的流量只经过混匀池一次，不重复计数。
- 全部算术使用 `fractions.Fraction`，中间分数存于 TEMP 表，逐 event 流式处理。
- 输出每个仍有正余量的 lot：`remaining_quantity`、来源数量 `source_quantity`（精确分数
  numerator/denominator 与分数字符串）、每单位该 lot 所含来源数量 `source_per_unit`
  （浓度而非有界比例：BOM 每件产出耗用多件来源时可大于 1）；报告头含来源
  lot、混匀模型、来源归属总量 `total_attributed` 与守恒校验。所有剩余 lot 的来源归属
  之和必须**精确等于**原料 receipt 原量（守恒断言在库内先校验再输出，不平即报错）。
- 结果可直接打印为单个 JSON，或经 `--output` 原子写文件（逐 lot 流式序列化，不缓存整表）；
  输入错误退出 2。

# 叶件销售承诺 v5

在修订库（v2）的同一 SQLite 库内新增叶件销售承诺（schema 升级到 v2，表见下）。
计划 JSON 直读模式、修订、排程与追溯的既有入口、退出码与输出保持不变；
旧的 v1 结构数据库在首次承诺相关写入时于同一事务内惰性迁移，旧 `report` 始终可读。

## 订单与标识契约

- 订单负载严格为 `{"order_id": <非负整数>, "lines": [...]}`；订单 ID 为整数、全局唯一。
  每行 `{line_no, candidates, quantity, due_period}`：`line_no` 为非负整数且单订单内唯一，
  `candidates` 为非空、去重的物料标识数组，**只允许叶件（无 BOM 子件的采购件）**，
  `quantity` 为 1..10^9 正整数（每行可在多个供给点间拆成任意整数份），
  `due_period` 为不越出周期范围的非负整数。单行候选上限 50、单订单行上限 50。
- 同 ID 同内容（规范化 JSON 的 SHA256 相同）重放幂等，返回当前版本与分配代号且不新建分配；
  同 ID 异内容拒绝；`release` 后订单以 `released` 状态保留，**ID 永不复用**，
  同 ID 重放不会复活旧承诺。

## 供给与版本优先

- 承诺可用供给仅为叶件**期初库存（period 0 的 on_hand）与 scheduled_receipt**；
  计划到货（planned_receipt）是无限产能建议，不构成可承诺供给。
- 先按物料逐期做版本毛需求 FIFO：每期毛需求（独立需求加父件计划下达贡献，直接取自与
  修订库同一个净算内核）从最早的未用供给点扣减；当期供给不足时欠量结转，继续消费
  后续期间的最早到货。版本毛需求永远先于任何承诺得到满足，结转欠量记入
  `version_shortfall`。
- FIFO 之后的剩余供给才参与订单分配；一行只能取候选物料中**供给期不晚于交期**的供给点。

## 联合分配与确定性

- 全部活跃订单一次性联合分配，**禁止逐单贪心**：新订单不得通过先到先得挤占既有承诺，
  可行性由一个二部 b-匹配最大流判定（行需求 → 合格供给点容量）给出充要条件，
  贪心失败从不作为无解依据。
- 变量按 `(订单ID, 行号, 物料, 供给期, 供给类型)` 定序；逐变量固定「仍存在可行补全」的
  最大取值（可行性关于强制量单调，二分求最大），因此首解即该定序下的**字典序最大**
  分配向量。供给点内部按期排序，同期期初库存先于 scheduled_receipt。
- `reserve` 全单成功或全拒：先在事务内写入订单再从零重算全部活跃订单的联合分配，
  无解则连同订单行一起回滚，不留半单、不动既有分配。`release` 把订单置为 released、
  删除其分配并对剩余活跃订单从零再分配，状态变更与新分配同事务提交。

## 修订交互与持久化

- `apply` 合并 demand/receipts delta 后，在同一事务内按修订后的供需从零重算承诺；
  若任一有效（活跃）承诺在新供需下失配，**整笔修订拒绝**（退出码 3），版本、订单、
  分配与头指针均保持在已提交状态。输入单元格、批次、版本、快照、订单、分配同事务提交，
  提交前终止无半版。
- 每次重算生成单调递增的分配代号 `allocation_generation`；当前分配在
  `sales_allocation`（全量替换），历代号在仅追加的 `sales_allocation_history`
  中与当时计划版本绑定保留，供审计核对。
- `availability` 以只读连接打开，逐供给点输出 `quantity`、版本毛需求已耗
  `version_requirement_consumed`、已承诺 `committed` 与余量 `remaining`，恒满足
  `quantity = version_requirement_consumed + committed + remaining`；不会触发迁移。

## 全量重算与增广改配的权衡

本实现刻意选择每次 reserve/release/apply 都从 FIFO 残量与全部活跃订单**全量重算**：
结果只依赖已持久化的输入与订单，可直接由库内数据独立复算核对，历代号审计链清晰，
多供给竞争与撤销改配的正确性由最大流充要判定保证。其代价是耗时随活跃订单与供给点
增长而上升；增广（局部改配）通常更快，但撤销一个到货后需要级联重排多订单、多物料，
增量状态难以独立审计，故在当前单操作者、受限规模（活跃订单 ≤200、行 ≤1000）下不采用。


