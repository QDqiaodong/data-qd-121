# data-qd-121 · 时段物料需求计划

Time-phased net requirements planning with shared components。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/material-plan --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理；旧 JSON 直读模式不承诺并发发布或断电恢复，
SQLite 修订模式以单事务保证竞争仅一成功、提交前终止无半版。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/material_plan/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## SQLite 增量修订

`store.py` 在同一净算内核上提供仅追加的版本库（标准库 sqlite3）；BOM 与物料策略在 init 后固定，
修订仅接受 demand/receipts 的有符号 delta。规则、事务边界与增量重算语义见
[数据契约 · SQLite 增量修订](docs/contract.md)。

```sh
.venv/bin/material-plan init --source fixtures/input.json --database run/plan.db --output run/v0.json
printf '{"demand":[{"item":"P","period":3,"delta":2}]}' > run/batch.json
.venv/bin/material-plan apply --database run/plan.db --batch run/batch.json \
  --batch-id batch-0001 --base-version 0 --output run/v1.json
.venv/bin/material-plan report --database run/plan.db --version 0   # 任一旧版本仍可读
.venv/bin/material-plan versions --database run/plan.db
```

同 ID 同内容重放返回原版本；同 ID 异内容或基版本不是当前头均拒绝（退出码 3，其余输入错误退出 2）。
输入单元格、批次、快照、逐项逐期差额、recomputed_items 与头指针同事务提交，
提交前进程终止不会留下半版。增量只重算直接修改物料及计划下达实际变化传播到的后代，
传播在计划未变化处停止；每个版本的测试都与独立全量净算逐字段比对，并断言无关物料未重算。
原 `--source/--output` JSON 直读直出行为完全保持。

## 有限产能排程

`schedule` 子命令只读指定版本（默认当前头）的计划与一份资源日历 JSON，把非叶物料的非零
planned_receipt 落成不可拆装配作业：数量与最迟完工期固定，日历配置的 1..8 期工时替代
lead_time，各资源逐期容量 0/1（0 即停机）。限 32 期、12 个作业。同期内先到货/完工、再开工
按 BOM 扣件、再扣独立需求，库存不得为负；叶件 planned_receipt 为固定采购供给。回溯搜索联合
资源与物料约束，求解或完备判无解（贪心失败不算无解），多解按（最迟完工期,item）作业序的开工
向量取字典序最小。规则详见 [数据契约 · 有限产能排程](docs/contract.md)。

```sh
printf '%s' '{"jobs":[{"item":"P","resource":"line-a","duration":1}],
  "resources":[{"id":"line-a","capacity":[1,1,1,1]}]}' > run/calendar.json
.venv/bin/material-plan schedule --database run/plan.db --version 0 \
  --calendar run/calendar.json --output run/schedule.json
```

报告含版本、逐作业开工/完工、逐期资源占用与逐物料库存账；无解输出结构化结果（退出码 0），
输入错误退出 2 并保留旧输出。排程不写入版本库，修订后任一旧版本仍可排程。

## 叶件销售承诺（reserve / release / availability）

`reserve`/`release`/`availability` 在同一个修订库内提供叶件销售承诺（schema 升至 v2，
旧库首次承诺写入时在同事务内惰性迁移，旧 report 与旧版本始终可读）。订单以整数 ID 标识，
负载含多行，每行给候选叶件物料集、整数数量与交期，数量可在多个供给点间拆整数份。
同 ID 同内容重放幂等、异内容冲突；`release` 后 ID 永不复用、重放不复活。

可承诺供给仅为期初库存与 scheduled_receipt。先逐期 FIFO 满足版本毛需求（欠量结转、
优先于一切承诺），剩余供给再对**全部活跃订单联合分配**，只取不晚于交期的候选供给；
可行性由二部最大流判定，杜绝逐单贪心，多解按 `(订单ID,行号,物料,供给期)` 分配向量取
字典序最大。`reserve` 全单成功或全拒；`apply` 按修订后供需在同事务内重分配，任一活跃
承诺失配则整笔修订拒绝（退出码 3），版本、订单、分配同事务。`availability` 只读输出
逐供给点的版本耗用、已承诺与余量。规则与字段见
[数据契约 · 叶件销售承诺 v5](docs/contract.md)。

```sh
printf '%s' '{"order_id":1,"lines":[{"line_no":0,"candidates":["A","B"],
  "quantity":4,"due_period":3},{"line_no":1,"candidates":["A"],
  "quantity":1,"due_period":3}]}' > run/order.json
.venv/bin/material-plan reserve --database run/plan.db --order run/order.json
.venv/bin/material-plan availability --database run/plan.db
.venv/bin/material-plan release --database run/plan.db --order-id 1
```

刻意采用每次全量重算而非增广改配：结果可由库内数据独立复算、历代号审计链清晰，
代价是随订单增长变慢；增广更快但撤改与多供给竞争难以审计。权衡说明见数据契约 v5。

## 实际批次追溯（build / recall）

`trace.py` 在版本库之外提供独立的**实际批次追溯库**（标准库 sqlite3），新增 `build` 与
`recall` 两个子命令（也可经 `material-trace` 入口调用）。`build` 只读计划库的物料与 BOM
（`mode=ro`，绝不写入），把乱序事件 CSV（`type,event,lot,item,quantity`，最多 20 万行）
流式导入新建追溯库；暂存、校验与依赖解析全部在 SQLite 内完成，明细与依赖边不整体装入内存，
20 万行构建的 Python 峰值堆内存约 2 MiB。receipt 是叶件唯一新 lot；同一生产 event 可领用多个
旧 lot、产出同一父件多个新 lot，各组件总耗用必须恰为产量 × BOM 用量。未知件、重复产出、
循环、悬空引用、累计超领等一律拒绝，整批在隐藏临时库内单事务完成后原子改名发布，失败不留半仓。
生产顺序由 lot 依赖经库内批量 Kahn 扫描确定。规则与输出字段见
[数据契约 · 实际批次追溯 v4](docs/contract.md)。

```sh
.venv/bin/material-plan init --source fixtures/input.json --database run/plan.db
.venv/bin/material-plan build --plan-database run/plan.db \
  --events fixtures/trace_events.csv --trace-database run/trace.db
.venv/bin/material-plan recall --trace-database run/trace.db --lot a1 \
  --output run/recall-a1.json
```

`recall` 指定原料 receipt lot，按完全混匀把其来源数量随部分领用与产出比例传播，用
`fractions.Fraction` 给出每个剩余 lot 中来源数量的**精确分数**与每单位浓度 `source_per_unit`；
先分拆再汇合不重复计数，
所有剩余 lot 的来源归属之和精确等于原料原量（库内先做守恒断言再输出）。追溯库以只读连接打开，
不写任何持久数据；结果可直接打印为 JSON 或经 `--output` 原子流式写文件。原 JSON 直读模式、
修订库与排程的入口、退出码和输出完全保持。

## 业务与样例结果

P期3需求4，库存1，计划到货3、期2下达；A期2毛需6，按批量5补货后结余4。
contracts.py校验BOM与拓扑，model.py进行期间净算及父件需求来源展开。负下达期明确告警。
