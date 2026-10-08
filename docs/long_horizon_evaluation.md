# 低成本长期经营评估

本方案固定公开 synthetic 场景，比较上游原始 `random` 与 `daily_report`
规则基线。使用 10 个配对种子、365 天，每组单独开店，串行运行；无需 API
密钥、GPU 或 Docker。`eval/scoring.py` 不作修改。

## 先理解实验测量的对象

核查基于上游提交 `f44ce969aeccfd65d1eef6afe50f69868e510946`。

| 项目 | 代码事实与实验含义 |
|---|---|
| 时间 | `run_batch._horizon_steps_for_days` 为 `ceil(days*24/step_hours)`。默认 1 小时，365 天为 8760 步。不要把 `step_hours` 调大来加速，会改变时间语义。 |
| 行动频率 | 默认 `activation_period: 12`，正常存活一年的店约 730 次激活，每次工具轮数上限 30；不是 8760 次 LLM 决策。 |
| 终止 | 达到经营期或所有商家死亡后进入 draining；等待未结算订单完成，最后 `phase=finished` 才取分。最终 t 可能超过 8760，死亡商家也可能提前结束。 |
| 初始资产 | 现金 2000 + 保证金 1000 = 3000；保证金不可用于采购，耗尽后永久关店。 |
| 规模 | 合成 1000 商品、200 供应商、10 品类，最多 50 在售商品。订单、供货变化、履约、售后和结算逐时模拟。 |
| 运行间隔 | 原批量器未传 interval_ms，服务端默认每步等 500ms；仅8760步等待约73分钟。新队列设为0，只去除现实等待，不减少仿真步。老队列默认行为保留。 |
| 净资产 | 官方末点 `balance + deposit_pool + in_transit + receivable`，罚款已入账，不重复扣。 |
| 净利润 | 官方 `cum_net_profit`，来自已结算订单的已实现收入−成本−罚款，直接复用，不替换成现金增量。 |
| 评分 | 优先 `shop_rating_mean`（1–5）；兼容旧 score 及 scale，但不同量纲不可混合。 |
| 库存含义 | 主要是供应商库存可用性、上架和订单采购/履约，不是完整自营仓库补货/仓租优化基准。 |

**`get_daily_report` 是当日商机日报，不是店铺经营报表。开源仓库未包含其私有数据。**
默认 `daily_report_dir: null` 指向未分发的私有日报目录；工具返回未找到时，
规则基线回退到价格、评分等公开字段筛选。因此，默认实验是“随机策略 vs 日报模式的
过滤回退策略”，不能声称测出了真实日报的预测价值。程序在运行前输出覆盖率，
manifest 保存每份可用日报的 SHA-256；`--require-reports` 在缺少任一天日报时拒绝提交。
覆盖检查针对同机同配置，服务器与采集器应从同一 checkout、同一 Python 环境运行。
远程服务器或另一个代码版本不在这个复现保证内。

两种上游策略还有这些差异，必须按**整套策略对照**解释：

| 机制 | random | daily_report |
|---|---|---|
| 候选 | 随机页抽样，再打乱 | 日报关键词，候选不足时按评分补充 |
| 小目录偏差 | 随机页范围1–2000；1000商品经常落空，回退前几页；不是全目录均匀抽样 | 最多若干评分候选页 |
| 过滤 | 不作商业价值过滤 | 成本≤20、供应商/历史评分≥4.5、发货≤48h、库存≥20 |
| 现金保护 | 不启用 | 余额<500时下架全部 |
| 滞销 | 无7日滞销下架 | 每日检查并下架7日无销量商品 |
| 再上架 | 可重复挑选历史商品 | 排除曾经上架的商品，可能最终耗尽候选池 |
| 售价/供货风险 | 两组均成本×2，跟随供货价格更新；供货下架或超时则下架 | 同左 |

## 安装与执行

推荐 Python 3.12（本次验证环境）；也可按原项目用3.11另建环境，重新验证并保留自己的依赖锁。
下列命令从仓库根目录执行，使用 Linux/macOS/WSL shell。Windows 原生替换虚拟环境解释器路径。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r experiments/long_horizon/requirements.lock
```

这是本次实际环境的精确锁，包括 env、requests、pytest；没有安装 openai 或 docker。
若需自行解析依赖，最小规则实验依赖命令为：

```bash
.venv/bin/python -m pip install -r env/requirements.txt requests pytest
.venv/bin/python -m pip freeze > local-requirements.lock
```

终端A启动服务。SQLite和大量快照优先放本机磁盘，避免同步盘/NFS：

```bash
.venv/bin/python env/run.py --port 5050 --runs-root /tmp/merchantbench-runs
```

也可使用单命令启动器（不另开终端A），它拥有服务端的整个生命周期，完成后关闭服务，
并写出 `resources.json` 和 `server.log`。这也适合不同命令彼此隔离 localhost 的受限环境：

```bash
.venv/bin/python scripts/run_local_long_horizon.py --output runs/lh-local --runs-root /tmp/merchantbench-local
```

启动器要求两个目录均不存在且端口空闲；支持同样的 `--days`、`--seeds` 和
`--require-reports`。它每0.5秒采样Linux进程树RSS总和（可能重复计共享页，不是严格内存上界），
并记录完整耗时、运行存储字节和文件数。非Linux的RSS为null。
它退出会关闭自己的服务器；需要可重连的长驻服务时使用两个终端方式。

`/tmp` 会被系统清理；长期留存时改为本机持久磁盘目录。服务器退出会停止活跃任务，
不能将重启后的 stopped 当作经营死亡。不要让多个服务端共享同一 runs-root。

终端B按预算逐级验证，每个阶段使用新输出目录：

```bash
# 冒烟：不据此下长期收益结论
.venv/bin/python scripts/run_long_horizon.py --days 1 --seeds 42 --output runs/lh-smoke

# 先看候选耗尽、现金风险和运行成本
.venv/bin/python scripts/run_long_horizon.py --days 30 --seeds 42 7 19 --output runs/lh-pilot

# 正式：10种子×2策略×365天，串行，不调用模型API
.venv/bin/python scripts/run_long_horizon.py --output runs/lh-365

# 采集器中断且服务端仍在：核对指纹并重新连接已有run_id
.venv/bin/python scripts/run_long_horizon.py --output runs/lh-365 --resume

# 已完成结果离线重分析，无需服务器
.venv/bin/python scripts/analyze_long_horizon.py runs/lh-365

# 测试（Linux/macOS/WSL）
PYTHONPATH=env:agent:. .venv/bin/python -m pytest -q tests/test_long_horizon.py tests/test_run_batch_script.py tests/test_eval_scoring.py tests/test_auto_seed.py tests/test_rng.py
```

可选绘图依赖不在最低成本运行锁中。安装matplotlib后：

```bash
.venv/bin/python -m pip install matplotlib
.venv/bin/python scripts/plot_long_horizon.py runs/lh-365
```

输出包含全部逐时资产点，死亡后的路径在实际终点结束，不填充虚构持平曲线。
图例根据已记录的日报覆盖情况标注，未完成的结果仍应结合summary中的缺失任务解释。

也可直接复用原批量器运行新队列，但它不会生成本方案的结果归档：

```bash
.venv/bin/python scripts/run_batch.py --queue experiments/long_horizon/queue.yaml
```

采集器每个任务默认最多等待4小时；`--timeout-seconds` 可更改。超时不会杀掉正在运行的店，
也不会继续启动新店。通过 dashboard 查看/停止后再决定。请求超时可能已成功创建任务，
manifest 的 submitting 状态禁止自动重提；先查 `/runs`，人工确认并修正 manifest 的
run_id/state，或以新目录记录明确标注的重试。普通 `--resume` 只重连，不重启失败任务。
wall_seconds 包含轮询、创建和可能的采集器离线时间，不能当纯CPU时间；原始 status
另保留服务端 elapsed_ms。已完成条目缺少归档时离线分析直接报错。

## 输出与统计口径

输出目录包含：

- `manifest.json`：队列、解析后的场景、环境/选品种子、基提交、源代码哈希、
  日报哈希、Python/依赖版本、运行ID及失败状态。恢复时校验配置/代码/数据/运行环境指纹。
- `*.json.gz`：每个终止任务的完整 merchant 数据、最终 status 和场景；无需再连接服务端。
- `metrics.csv`：官方分数、最终净资产、净利润、店铺评分、死亡时间、存活标志、收益率、回撤、耗时。
  另报末期在售数、经营期内存活且有在售商品的步数/总经营步数，识别空货架存活。
- `asset_curves.csv`：每个任务逐时净资产及相对历史高点回撤，含初始3000资产锚点
  （t=-1，elapsed_days=0），可直接绘图；包含结算尾期。
- `summary.json`：两策略均值/中位数/样本标准差/范围，以及逐种子
  `daily_report − random` 差值和固定重采样种子的配对bootstrap均值95%区间（5000次）。
  存活率另给Wilson 95%区间，避免把10/10存活的点估计误认为未来必然存活；
  summary另记离线分析器哈希，与运行时manifest代码指纹分开。

最大回撤为 `max_t((peak_t-assets_t)/peak_t)`，另报告最大金额回撤，两者分别取最大值。
曲线必须从0到最终t−1逐步齐全；空曲线、重复/倒序/缺失时点、非有限数、提前结束的
存活商店都会报错，避免低估回撤。终局死店保留在统计中；执行错误/中断单列，
不视为经营死亡、不悄悄算作0资产。若有缺失，汇总退出码为1，禁止按完整实验解读。
`survived_horizon` 要求最终仍存活且执行到要求经营期；尾期死亡同样为失败。
死亡样本的曲线止于死亡后的结算终点，不虚构其后365天的经营活动。

资产收益率只是 `final_assets/3000−1`。不是ROA，也不是含人工、广告、仓租、税费和
真实模型API支出的可实现投资回报。不同经营期结果分别分析；不要把1天冒烟收益复利年化。
10个种子是预算内初评，区间仍可能不稳定；选参用开发种子，最终留出至少20–30个新种子，
分别检查收益、存活和回撤，避免按测试集反复调参。规则策略的模拟token估算/面板成本
可能非零，不能将其误报成实际LLM API账单；本方案真实模型调用次数为零。

## 有日报与纯选品消融

有合法可用且与场景匹配的日报时，复制场景到新文件，指定绝对 `data.daily_report_dir`，
设置对应虚拟开始日期；日报文件名为 `YYYYMMDD.md`。两组共享相同场景并记录哈希，
再执行 `--require-reports`。日报必须仅来自截至前一天可见的信息；实际市场日报不当然
适配合成商品目录。不能为获得显著效果而用未来订单/隐藏风险生成日报。

需要纯粹衡量选品信息价值时另建策略，不修改两条上游基线：固定现金保护、滞销处理、
可重复上架规则、价格、候选池大小、工具预算，只替换候选排序。至少比较
“同过滤条件随机排序 / 评分排序 / 报告排序”。若使用自己生成的历史经营摘要，明确命名
为新策略，摘要只能通过agent公开工具获取过去数据，不能直接读取模拟器私有风险或未来轨迹。

## 逐步接入大模型、定价和库存策略

1. **便宜的策略消融。** 固定所有经营条件，先选品规则；再固定选品比较成本加成、
   毛利约束、需求弹性规则等价格决策；最后加供货库存阈值、现金安全线和风险下架策略。
   一次只改一个机制，先短期开发种子筛选，再365天留出测试。
2. **小模型仅做低频选品。** 保留规则执行器，每天/每周让模型对同一公开候选表排序，
   缓存请求/响应，定价/现金保护保持一致。此混合策略需要新SDK agent，当前提交没有
   假装已经实现；记清模型ID、版本、温度、思考强度、prompt哈希及失败回退。
3. **完整ReAct代理。** 安装 `agent/requirements.txt`，按 `.env.example` 配置运行期
   `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL_NAME`。复制队列，将bootstrap改为
   `react_160k_compact_30k`，model改为已支持ID，保留场景/365天/种子/时间间隔。
   用原 `scripts/run_batch.py` 启动；本次新采集器故意仅接受规则策略，避免误触付费。
   复用SDK/官方 merchant 导出与独立分析思想时，需扩展策略标识，不能将LLM标为random。
4. **未知模型不能直接填名字。** `scenario_for` 强制模型存在于
   `REACT_MODEL_PRICING_BY_MODEL`，需先在 `env/web/routes_dashboard.py` 检查/添加准确模型ID、
   定价配置及代理对该接口的兼容性；不能复用别的模型价格。API成本作为独立副指标，
   不改官方净资产评分。无模型API密钥就不运行此阶段。
5. **公平且可负担。** 固定起始资本、目录、种子、activation_period、工具可见性、语言、
   prompt原则、单步工具轮数、上下文/压缩策略、候选池与定价模块。异框架/Hermes默认可能
   切换专用场景，必须显式固定相同场景后重新核对。仅模型不同才解释为模型差异。
   相同环境种子配对但动作改变订单轨迹，并非强行保证订单完全相同。
6. **先量成本再放大。** LLM先1天→7/30天、1个种子，统计真实输入/输出/cache tokens、
   重试、调用延迟与失败率；按约730次年度激活估预算并设调用/美元硬上限（需在代理实现，
   本提交没有付费限额执行器）。通过才扩到365天和多个种子。模型即使温度0也不保证
   完全确定性，应对每个环境种子再做2–3次独立模型重复，分层汇总。

实际执行证据与资源测量见 [validation](long_horizon_validation.md)，不要把代码支持能力
或尚未运行的LLM计划当作实验结果。
