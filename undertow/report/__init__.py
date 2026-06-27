"""报告层：把分析结果渲染成人类可读的产物。

与分析层解耦——只吃 analyze 层的结果对象，不做任何计算/取数。
    markdown  终端/纯文本 Markdown 报告（analyze / gamma / flow / backtest 各命令）
    html      自包含 HTML 综合研判报告（内嵌 SVG，可直接浏览器打开）
    viz       手写 SVG 图表（纯标准库，不依赖 matplotlib）
"""
