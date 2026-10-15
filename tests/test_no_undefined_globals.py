"""函数体里引用了未定义的全局名 → 只有走到那一行才 NameError，若外层 except 吞掉就成了静默失败。

起因：46d92ddf 把 report 的 `on_or_before=today if replay else None` 复制进 cmd_flow，而 flow 里没有 `replay`；
`undertow flow` 从此每个品种都 NameError，被 per-instrument except 吞成「没有可用结果」（2026-10-15 事前判断时才发现）。
这里静态扫描 undertow 包内所有模块级函数（含嵌套函数/推导式）的 LOAD_GLOBAL，名字必须在模块或 builtins 里。"""
import builtins
import dis
import importlib
import pkgutil
import types

import undertow


def _walk(code, glb, owner, bad):
    for ins in dis.get_instructions(code):
        if ins.opname == "LOAD_GLOBAL":
            n = ins.argval
            if n not in glb and not hasattr(builtins, n):
                bad.add(f"{owner}: {n}")
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            _walk(c, glb, owner, bad)


def _check(f, mod, owner, bad):
    # 只查本模块源码里写的函数（dataclass 等生成的方法 globals 不是本模块，跳过）
    if isinstance(f, types.FunctionType) and f.__globals__ is vars(mod):
        _walk(f.__code__, f.__globals__, f"{mod.__name__}.{owner}", bad)


def test_no_undefined_globals_in_package():
    bad = set()
    for info in pkgutil.walk_packages(undertow.__path__, "undertow."):
        mod = importlib.import_module(info.name)
        for k, v in list(vars(mod).items()):
            if isinstance(v, types.FunctionType):
                _check(v, mod, k, bad)
            elif isinstance(v, type) and v.__module__ == mod.__name__:
                for kk, vv in vars(v).items():
                    _check(getattr(vv, "__func__", getattr(vv, "fget", vv)), mod, f"{k}.{kk}", bad)
    assert not bad, sorted(bad)
