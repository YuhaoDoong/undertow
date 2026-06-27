"""公共核心层：跨层共享的数据模型、配置、时钟。

不依赖 collect / analyze / report 任何一层，可被它们自由引用而不成环。
    models  纯数据结构（CotReport / OptionsSnapshot / OptionContract 等）
    config  实例与数据源注册表（读 config/instruments.json）
    clock   美东交易日时钟（用户在新加坡，市场在美国）
"""
