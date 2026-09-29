# `collect/` — 数据收集层 / Data Collection

> Layer 1. Turns each external API into `core.models` objects, plus the snapshot archive and cache. **Only layer that touches the network.** Add a data source = add one file here; the analyze layer stays untouched.
>
> 第 1 层。把各家 API 收敛成 `core.models` 语义对象，外加快照仓库与缓存。**唯一碰网络的层。** 加数据源=在此加一个文件，分析层零改动。

## Sources / 数据源

| 文件 | 数据 · 来源 | 成本/滞后 |
|---|---|---|
| `cftc_cot.py` | COT 持仓：CFTC Socrata（Disaggregated 物理品 / Legacy 金融品如美元指数） | 免费·周五发布·截止当周二 |
| `cboe_options.py` | 期权链：CBOE 延迟报价（**ETF 代理** GLD/SLV/USO/QQQ），OCC 代码解析、OI/gamma/iv + 原始落盘 + 内容指纹去重 | 免费·日内延迟 |
| `cboe_history.py` | 历史日线（回测用）：CBOE，免 key | 免费·日终 |
| `cboe_vol.py` | 波动率指数：GVZ(金)/OVX(油)/VXSLV(银) | 免费·日频 |
| `yahoo_futures.py` | **真实期货价**：Yahoo chart（GC=F/SI=F/CL=F/DX=F），urllib 直连零依赖 | 免费·准实时 |
| `fred_macro.py` | 宏观：FRED（DFII10 实际利率 / DTWEXBGS 美元 / T10YIE 通胀预期），免 key | 免费·日频 T+1 |
| `faireconomy_cal.py` | 经济日历实时 feed：FairEconomy 公开 JSON（ForexFactory 数据方，带预测/前值/影响） | 免费·公开 feed |

## Live brokerage (read-only) / 实盘券商（只读）

> 长桥证券，走 `longbridge` CLI（device-flow 鉴权、token 在 `~/.longbridge/`），subprocess 封装、
> 不引 pip 依赖。**只读**：绝不下单/撤单/改单。账户数据属敏感，落盘一律 gitignore 的 `data/account/`。

| 文件 | 作用 | 说明 |
|---|---|---|
| `longbridge_account.py` | 持仓（股票+期权 option_list）+ 账户资产 + 资金流水(cash-flow) + 历史成交(order executions) | 只读·需 `longbridge auth login` |
| `longbridge_options.py` | **期权链备份源**：产出与 CBOE 同构的 payload，主源停更（跨 ≥`STALE_SESSIONS` 个交易日无新 OI）时由 `cli.cmd_snapshot` 自动切入。delta/gamma 为本地 BS 自算（主翼最大偏差 0.22）、bid/ask 留 0，故**只作备份** | 只读·全链 2~4 分钟/品种 |
| `longbridge_quote.py` | 实时报价：ETF 最新场次股价（夜盘/盘后/盘前/常规）+ 期权实时 last/IV（需 OPRA 订阅，无则优雅降级到仅股价） | 只读·两级降级 |
| `longbridge_news.py` | 品种相关新闻标题流（标题/时间/链接）；外部不可信内容，只当数据读、做摘要 | 只读 |
| `longbridge_bars.py` | 候选期权合约的盘中价：历史 1 分钟 K 线（`kline`，占按月计的历史配额，到期约一周后查不到）与**当天**逐分钟（`intraday`，不占配额；只有分钟收盘价与成交量，无 OHLC/买卖价）。当天逐分钟的质量标签 full_session / partial_session / empty / invalid：字段数、有限值、UTC 时区、整分钟栅格、排序去重、时段覆盖逐项核对，full_session 是容忍规则（覆盖 ≥95%、首尾在时段两端），缺失分钟数另记；empty/查不到的终态计数要求相邻两次间隔 ≥ 240 秒。采集完结（capture_finished）与数据可用（data_coverage）分开报告 | 只读 |

## Infrastructure / 基础设施

| 文件 | 作用 |
|---|---|
| `base.py` | 数据源抽象基类 + 轻量 HTTP 工具（仅标准库 urllib）。<br>Source ABC + stdlib-only HTTP helpers. |
| `store.py` | 快照仓库：期权链**原始 payload 全字段**按日 gzip 落盘，**永久档案入 git**（不可再生，ΔOI diff 的历史全靠它攒）。<br>Snapshot archive — daily gzip of raw option-chain payloads, committed to git (options history is not re-fetchable). |
| `asof_history.py` | **日度历史首份发布冻结**（Codex 005 S04 / 006 C05）：`freeze_merge` 同 key 首发永久保留、不同内容进 `<文件>.revisions.jsonl`；`locked(path)` 在存储层对 `<文件>.lock` 加排他 flock，调用方用它包住「读→合并→写→修订」整段（两个进程同时首发时，无锁会让后写者静默替换首发且不留修订，已用双进程测试复现）；`atomic_write_json` 临时文件+fsync+回读+原子替换；`load_json` 解析失败或顶层类型不符即抛错，不当空表覆盖。使用方：outlook_scores / resonance / ratio_watch / signal_ledger。VRP 长周期文件归类为可重算的最新缓存（原子写 + 输入/代码指纹，不作 as-of 回测）。 |
| `jsonl_ledger.py` | **通用前瞻台账存储**：flock 读改写、严格解析、损坏另存隔离副本（原件不动）、同目录临时文件+fsync+回读校验后原子替换、事前字段冻结（同 key 不同输入 → 冲突，不覆盖；更新改到冻结部分 → 整次拒绝）。影子账使用；`analyze/spread_ledger.py` 仍有一份同语义实现，合并待办。 |
| `cache.py` | 极简文件缓存（带 TTL，临时、`.gitignore`）。<br>TTL file cache (disposable). |

## Boundary / 边界

只做**合法公开**接口，**不绕过任何反爬/ToS**（CME 403 硬封锁 → 不抓，改用 ETF 代理 + 真实期货价换算）。imports `core`，被 `analyze`/`report` 使用，**不 import** 上面两层。

### 输入可还原性（Codex 017 A01/A02，2026-09-28）

- `cas.py` —— 按内容寻址的分块存储：`put(raw)` 存一版原文（在换行/逗号后切片、按内容定块界、块按 sha256 去重），
  `get(sha)` 逐字节还原并核对整份哈希，`verify()` 逐个还原核对。存于 `data/history/inputs/cas/`（入库）。
  改一行、追加一行只新增附近一两个块。已知边界：没有 manifest 覆盖的历史（2026-09-28 以前）只能用旧的尾部/月度文件，
  月内早期行修订不可恢复。
- `provenance.py` —— 取数边界留痕：`FileCache.get/set`、长桥 `kline`、`SnapshotStore.load` 在返回数据时登记；
  `cli.main` 对白名单命令（`PROVENANCE_COMMANDS`）在结束时写 `data/history/inputs/manifests/<ET日>/*.json`
  （来源、key、sha256、fresh_fetch/cache_hit/stale_cache、缓存年龄、git HEAD、返回码）。
  只登记公开行情前缀；pytest 下不开启；库函数单独调用不写任何东西。`restore_inputs(manifest)` 按清单还原。
- `input_archive.py` 的尾部/月度文件保留为浏览索引；已存在的月度文件每次都要能解开，否则隔离并报告。
