"""信号日当天，价格有没有走到墙上 —— 用户问的「看跌期权的点位位置是否达到」。"""
import json, pathlib, sys
from datetime import date, timedelta
sys.path.insert(0, '.')
from undertow.analyze.gamma import analyze_gamma, persistent_walls
from undertow.cli import snapshot_from_payload
from undertow.collect.longbridge_kline import fetch_bars
from undertow.collect.store import SnapshotStore
from undertow.core.config import load_config

SIG = json.loads(pathlib.Path('data/history/strong_signal_days.json').read_text())
cfg, store = load_config(), SnapshotStore()
sym_of = {k: i.options.symbol for k, i in cfg.instruments.items() if i.options}
BARS = {}
for k, sym in sym_of.items():
    try:
        BARS[k] = fetch_bars(f"{sym}.US", period="1h", count=200)
    except Exception:
        pass


def _pw(d):
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


print("信号日当天，价格是否触及对应方向的墙\n")
print(f"{'日期':<12}{'品种':<7}{'方向':<6}{'开盘':>8}{'目标墙':>8}{'需走':>8}"
      f"{'当日极值':>9}{'达标?'}")
print("-" * 74)
tot = hit = 0
for r in SIG:
    k, d = r['inst'], r['date']
    db = [b for b in BARS.get(k, []) if str(b['ts'])[:10] == d]
    if not db:
        continue
    dt = date.fromisoformat(d)
    p = store.load("options", sym_of[k], dt)
    if p is None:
        continue
    try:
        ga = analyze_gamma(snapshot_from_payload(p, k, sym_of[k]), multiplier=1.0,
                           proxy_quality=cfg.instruments[k].options.proxy_quality,
                           today=_pw(dt), horizon_days=45)
    except Exception:
        continue
    bear = r['dir'] == '看跌'
    wall = ga.put_wall if bear else ga.call_wall
    if not wall or wall <= 0:
        continue
    o = db[0]['open']
    ext = min(b['low'] for b in db) if bear else max(b['high'] for b in db)
    need = (wall / o - 1) * 100
    got = (ext / o - 1) * 100
    ok = (ext <= wall) if bear else (ext >= wall)
    tot += 1
    hit += ok
    print(f"{d:<12}{k:<7}{r['dir']:<6}{o:>8.2f}{wall:>8.0f}{need:>+7.1f}%"
          f"{got:>+8.1f}%   {'✅' if ok else '❌'}")
print("-" * 74)
if tot:
    print(f"当日触及目标墙：{hit}/{tot} = {hit/tot*100:.0f}%")
    print()
    print("⚠️ 墙大多离现价很远（见「需走」列），当日触及本就是小概率事件。")
    print("   把墙当日内目标位，多数时候等于不设目标。")
