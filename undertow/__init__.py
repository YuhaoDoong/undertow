"""undertow — 读懂价格表面之下的机构持仓暗流。

四大功能层（像 skill 一样按职责分明的模块组织）:
    core      公共核心：models 数据模型 / config 注册表 / clock 美东时钟
    collect   数据收集层：各数据源适配器 + 快照仓库 + 缓存
                base/cftc_cot/cboe_options/cboe_history/cboe_vol/
                fred_macro/yahoo_futures/store/cache
    analyze   数据分析层：positioning/gamma/flow/macro/outlook/backtest/
                signals/blackscholes —— 只吃 core.models，与数据源解耦
    report    报告层：markdown 终端报告 / html 自包含研判报告 / viz 手写 SVG
    cli       命令行编排入口（python -m undertow <command>）

设计原则:
    - collect / analyze / report 三层 + core 解耦，各层可单独替换/测试。
    - 新增数据源 = collect 加一个文件；新增品种 = 改 config/instruments.json。
      （真·CME 期货期权数据源亦在 collect 即插即用，分析层零改动。）
    - 核心仅依赖标准库，保证可移植（便于封装成 Claude Code / Codex skill）。
    - LLM 不负责"算数"，只负责编排与解读；数值全部走确定性计算。
"""

__version__ = "0.2.0"
