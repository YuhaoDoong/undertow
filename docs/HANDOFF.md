# undertow 交接说明（2026-09-01 更新）

给**其他会话/其他人**快速接手用。系统性梳理，按「现在在做什么 → 怎么跑 → 已知的坑」组织。

## 0. 一句话现状

主攻**墙位卖方价差**策略。白银已激活（仅 put 侧），黄金/QQQ/TQQQ 待验证。
今天（2026-08-31~09-01）在这个策略上做了整整一轮参数实测，**并推翻了自己早先的多个结论**。

## 1. 必读的三份文件

```
memory/wall-credit-spread-strategy.md   策略六条口径 + 逐品种参数 + 最大保留
memory/output-is-skill-not-disclaimer.md 守则：产出是可用 skill，不是免责声明
docs/daily/2026-09-01.md                 最新日报（账户/结构/候选/已知问题）
docs/codex_review_2026-08-31.md           codex 审查全文（5 个 P0 + 11 个 P1）
```

## 2. 核心模块

```
undertow/analyze/wall_spread.py    墙位卖方价差 v2（当前主线，白银已激活）
undertow/analyze/credit_wall.py    v1，已停用（口径全错，保留作反例）
undertow/analyze/validation.py     验证登记簿——任何进入决策的判断必须在此登记 n/p
undertow/analyze/sizing.py         Kelly 仓位（不是固定百分比）
undertow/analyze/cost_gate.py      预期波动 vs 回本门槛
undertow/analyze/backmonth.py      远月异动扫描（R16，时间尺度隔离）
undertow/report/viz.py             wall_history_svg = 墙位历史图（尚未接进报告）
```

## 3. 回测脚本（全部可复现）

```
scripts/backtest_wall_lock.py    锁墙位版（用户手动策略的精确复刻）
scripts/scan_wall_spread.py      逐维扫描 --dim cap|confirm|width|dte|offset
scripts/grid_wall_spread.py      联合网格 240 组 + 邻域稳健性 + 边际效应
scripts/test_signal_exit.py      信号平仓（含距离条件）
scripts/backtest_credit_wall.py  v1 的可复现回测（证明 v1 为负）
```

逐笔账本落在 `data/backtest/*.jsonl`，含信号日/腿位/结算价来源/被排除计数。

## 4. 六条口径（违反即结论无效）

1. 墙 = **跨到期累计 OI** 最大行权价，band 限定搜索半径（≤14天抖动 16~19%；
   全到期会被深虚污染——GLD 8/11 470C/460C 各增 4 万张把墙顶到 460 而现价 402）
2. 破墙 = **到期日收盘**越过卖腿，不是盘中触及（差 3~4 倍）
3. 价差按**组合单中价**成交（让 25% 点差），不是两边吃满
   （后者把 SLV 55/54 一周价差的 $7 算成 $0）
4. **锁定墙位**，换墙才平；换墙可用 conf 平滑，但**平仓不能等 conf**
5. 价差宽度按**现价百分比**，且**每品种单独定**（$2 对 SLV 是 3.3%、对 QQQ 是 0.28%）
6. 按**日期簇**统计，检验**净收益>0** 而非胜率>50%（76% 胜率配 -0.43%/笔的反例在样本里）

## 5. 今天推翻的结论（别再重复）

```
✗ credit_wall v1 的「82% 胜率 +2.84%/笔」  → 四处方法论错误叠加，实为负
✗ 「墙会提前变薄」                         → 伪影，是墙位移动被算成 OI 变化
✗ 「墙位移动能预警破墙」                    → r≈0，且方向与直觉相反
✗ 「提前平仓更好」                          → 破墙率低时平仓等于还回时间价值
✗ 「短到期年化更高」                        → 早期结论，错口径下得出
✗ 「铁鹰比单卖好」                          → call 侧拖累，在上涨样本里不成立
✓ 「墙难破」                               → put 侧 100% 未破墙（但样本期在涨）
✓ 「卖在墙上比墙外好」                      → 两品种一致
✓ 「价差宽度要按品种定」                    → 最优值 SLV 3%、GLD 1%、QQQ 1%
```

## 6. 三个硬约束

- **纯标准库零依赖**，不引入第三方包
- **只读券商接口**，`order buy|sell|cancel|replace` 不得出现在仓库
- **付费作者内容绝不入库**：`article/`、`docs/screenshot/`、`docs/author_notes.md`、
  `docs/author_playbook.md`、`data/account/`、`data/soul/` 均已 gitignore。
  提交前必跑：`git status --short | grep -iE "article|screenshot|author|playbook|account/|soul/"`

## 7. 数据现状

```
CBOE 快照   GLD 40 份 / SLV 38 / QQQ 19 / 其余各 4（TQQQ/SPY/IWM/TLT 今天才接）
长桥        实时报价 ✓、期权链 ✓（但 chain 无 OI，要逐个 quotes 才有）
样本区间    2026-06-25 ~ 08-31，期间 GLD +10.5%、SLV +14.8%——全程单边上涨
```

**最大的未解问题：整套策略未经跌市验证。**

## 8. 每日流程

```
launchd 定时（~/Library/LaunchAgents/com.yuhaodoong.undertow.*.plist）
  daily        每日快照 + 报告
  lbintraday   长桥 OI 时效验证（全天 15 分钟采样）
  session      会话钩子
手动：python3 -m undertow.cli report [品种] [--no-snapshot]
      python3 -m undertow.cli account          实盘持仓只读复盘
      python3 -m undertow.cli journal --capture 抓当日成交
```
