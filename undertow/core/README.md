# `core/` — 公共核心 / Shared Core

> Layer 0 of the 4-layer package. Depends on **nothing else** in `undertow`; every other layer may import it freely. Pure data structures + shared utilities, no analysis logic.
>
> 四层架构的第 0 层。**不依赖**包内任何其它层，可被自由引用。只放纯数据结构与共享工具，不含分析逻辑。

## Files / 文件

| 文件 | 作用 |
|---|---|
| `models.py` | 数据模型：`CotReport` / `OptionsSnapshot` / `OptionContract` / `PriceSeries` 等纯 dataclass，把各数据源收敛成同一套语义模型。<br>Pure dataclasses — the semantic model every source normalizes into. |
| `config.py` | 读 `config/instruments.json`（品种注册表）与路径解析。加品种=改 JSON，不动代码。<br>Loads the instrument registry + resolves paths. Add an instrument = edit JSON. |
| `clock.py` | 统一时钟：**以美东（America/New_York）为基准**算交易日。用户在 SGT，本机日期比美东快约半天，故"今天"统一锚美东，避免与真实交易日错位。<br>Single clock anchored to **US-Eastern** trading days (user is in SGT). |
| `calendar.py` | 事件日历：读 `config/calendar.json` + 倒计时/窗口过滤 → "事件雷达"（FOMC/数据/COT/OPEX）。<br>Event radar: reads the calendar JSON, does countdown/window filtering. |

## Dependency rule / 依赖规则

```
core  ←  collect  ←  analyze  ←  report
(imported by all; imports none of them)
```

`core` 被上面三层引用，但自己**绝不**反向 import `collect`/`analyze`/`report`。改这里要谨慎——牵一发动全身。
