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

## 业务与样例结果

P期3需求4，库存1，计划到货3、期2下达；A期2毛需6，按批量5补货后结余4。
contracts.py校验BOM与拓扑，model.py进行期间净算及父件需求来源展开。负下达期明确告警。
