"""trading_intel — 期货/期权持仓情报分析地基。

模块分层（便于架构调整 / 增加功能）:
    config        实例与数据源注册表（读 config/instruments.json）
    models        数据模型（CotReport / TraderCategory 等纯数据结构）
    cache         简单文件缓存
    datasources   数据源适配器（base 抽象 + cftc_cot 实现；后续可加 cme_options 等）
    analysis      分析层（positioning 计算 + signals 解读，不依赖数据源细节）
    report        渲染层（把分析结果渲染成 Markdown）
    cli           命令行入口

设计原则:
    - 数据源、分析、渲染三层解耦，各自可单独替换/测试。
    - 核心仅依赖标准库，保证可移植（便于封装成 skill）。
    - LLM 不负责"算数"，只负责编排与解读；数值全部走确定性计算。
"""

__version__ = "0.1.0"
