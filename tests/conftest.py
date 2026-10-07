"""全测试共享：长桥调用预算（undertow/collect/lb_budget.py）的锁与统计文件写到临时目录，不碰 data/logs。"""
import pytest


@pytest.fixture(autouse=True, scope="session")
def _lb_budget_tmp(tmp_path_factory):
    import os
    d = tmp_path_factory.mktemp("lb_budget")
    old = os.environ.get("LB_BUDGET_DIR")
    os.environ["LB_BUDGET_DIR"] = str(d)
    yield
    if old is None:
        os.environ.pop("LB_BUDGET_DIR", None)
    else:
        os.environ["LB_BUDGET_DIR"] = old
