# -*- coding: utf-8 -*-
"""quality.py — 时间序列数据质量检测（零依赖，可直接运行）

检测四类「不报错但会毁掉下游」的坏数据：
  1. 数据冻结：数值连续多日完全不变，时间戳却还在往前走
  2. 序列停更：某标的的数据比同批其他标的落后若干天
  3. 数据拼接：长时间冻结后突然跳变（换源后两段硬接）
  4. 数据缺失：时间戳不连续

用法：
  python quality.py          # 用内置合成数据跑演示
  python quality.py --json   # 机器可读输出

阈值说明（每个都有实测依据）：
  FLAT_DAYS   = 10  正常标的最长连续不变实测为 1 天，取 10 留 10 倍余量
  LAG_DAYS    = 1   正常标的相对池内最新时间实测恒为 0 天
  SPLICE_FLAT = 30  跳变前至少冻结 30 天才算「死段后复活」
  SPLICE_JUMP = 0.40 单日跳变阈值，实测极端案例可达 +264%
"""
import json
import sys

FLAT_DAYS = 10
LAG_DAYS = 1
SPLICE_FLAT = 30
SPLICE_JUMP = 0.40


# ---------------------------------------------------------------- 检测原语
def trailing_flat(vals):
    """从尾部往回数，连续多少个值完全相同。"""
    if not vals:
        return 0
    last = vals[-1]
    n = 0
    for v in reversed(vals):
        if v == last:
            n += 1
        else:
            break
    return n


def constant_runs(vals, min_len=2):
    """找出所有长度 >= min_len 的恒定段，返回 [(start, length), ...]。"""
    runs = []
    if not vals:
        return runs
    start = 0
    for i in range(1, len(vals) + 1):
        if i == len(vals) or vals[i] != vals[start]:
            if i - start >= min_len:
                runs.append((start, i - start))
            start = i
    return runs


def splices(vals, min_flat=SPLICE_FLAT, jump=SPLICE_JUMP):
    """找「长时间冻结之后突然跳变」的位置，返回 [(index, ratio), ...]。

    两条件同时成立才算：
      a) 跳变前至少有 min_flat 个值完全相同
      b) 单日相对跳变超过 jump
    只看 b) 会把真实的暴涨暴跌误判。
    """
    out = []
    for i in range(1, len(vals)):
        prev = vals[i - 1]
        if prev == 0:
            continue
        ratio = (vals[i] - prev) / prev
        if abs(ratio) < jump:
            continue
        # 往回数恒定段长度
        n = 0
        j = i - 1
        while j >= 0 and vals[j] == prev:
            n += 1
            j -= 1
        if n >= min_flat:
            out.append((i, ratio))
    return out


def has_gap(ts_list, step_sec):
    """时间戳是否连续。step_sec 为期望间隔（秒）。"""
    if len(ts_list) < 2:
        return False
    for i in range(1, len(ts_list)):
        if ts_list[i] - ts_list[i - 1] > step_sec * 1.5:
            return True
    return False


# ---------------------------------------------------------------- 判定
def check_series(sym, series, pool_latest_ts, step_sec=86400):
    """对单个标的做全项检查，返回问题列表（空 = 健康）。"""
    vals = [p['close'] for p in series]
    ts = [p['ts'] for p in series]
    issues = []

    flat = trailing_flat(vals)
    if flat >= FLAT_DAYS:
        issues.append({
            'code': 'FROZEN',
            'detail': '末尾连续 %d 个值完全相同（阈值 %d）' % (flat, FLAT_DAYS),
            'severity': 'high',
        })

    lag_days = (pool_latest_ts - ts[-1]) / step_sec if ts else 0
    if lag_days > LAG_DAYS:
        issues.append({
            'code': 'STALE',
            'detail': '最后一条数据落后池内最新 %.1f 天（阈值 %d）'
                      % (lag_days, LAG_DAYS),
            'severity': 'high',
        })

    sp = splices(vals)
    for idx, ratio in sp:
        issues.append({
            'code': 'SPLICE',
            'detail': '第 %d 条出现 %+.1f%% 跳变，且跳变前长时间冻结'
                      % (idx, ratio * 100),
            'severity': 'medium',
        })

    if has_gap(ts, step_sec):
        issues.append({
            'code': 'GAP',
            'detail': '时间戳存在断档',
            'severity': 'medium',
        })

    return issues


def scan(pool, step_sec=86400):
    """对整个池子做检查。pool = {symbol: [{ts, close}, ...]}"""
    latest = max(p[-1]['ts'] for p in pool.values() if p)
    result = {}
    for sym, series in pool.items():
        r = check_series(sym, series, latest, step_sec)
        if r:
            result[sym] = r
    return result, latest


# ---------------------------------------------------------------- 演示数据
def _mk_series(n, base=100.0, ts0=1700000000, step=86400):
    """造一段正常带波动的序列"""
    import random
    random.seed(hash(n) % 10000)
    out, px = [], base
    for i in range(n):
        px = px * (1 + random.uniform(-0.03, 0.03))
        out.append({'ts': ts0 + i * step, 'close': round(px, 4)})
    return out


def build_demo_pool():
    import copy
    n = 120
    pool = {}

    # 1 健康标的
    pool['HEALTHY'] = _mk_series(n, 50.0)

    # 2 冻结：最后 20 天数值完全不变
    s = _mk_series(n, 30.0)
    frozen_px = s[-21]['close']
    for p in s[-20:]:
        p['close'] = frozen_px
    pool['FROZEN'] = s

    # 3 停更：数据只到 90 天前
    s = _mk_series(n, 80.0)
    pool['STALE'] = s[:90]

    # 4 拼接：前 60 天冻结在 0.1，之后跳到 0.5
    s = _mk_series(n, 0.1)
    for p in s[:60]:
        p['close'] = 0.1
    for i, p in enumerate(s[60:]):
        p['close'] = 0.5
    pool['SPLICE'] = s

    return pool


def main():
    pool = build_demo_pool()
    bad, latest = scan(pool)
    as_json = '--json' in sys.argv

    if as_json:
        print(json.dumps({'pool': len(pool), 'bad': bad},
                         ensure_ascii=False, indent=2))
        return 0

    print('=' * 62)
    print('数据质量扫描')
    print('=' * 62)
    print('池内标的      : %d' % len(pool))
    print('判定有问题    : %d' % len(bad))
    print('-' * 62)
    for sym in sorted(bad):
        print('%-10s' % sym)
        for it in bad[sym]:
            print('    [%-6s] %s' % (it['code'], it['detail']))
    print('=' * 62)
    print('健康标的: %s' % ', '.join(s for s in pool if s not in bad))
    return 0


if __name__ == '__main__':
    sys.exit(main())
