# -*- coding: utf-8 -*-
"""dream — v9.18 三层：默认网络/做梦（DMN；REM 促进远程联想 Cai2009——起点必须先被 prime）；
梦里的弱连接一律走可失败预测回路，由世界裁决，不做统计固化。

rest 不再是"脑子一片空白"：从不久前被点亮过的概念（recent_active 里 priming 窗口内的）
按新近度加权挑一个起点，沿弱边（w ≤ DREAM_WEAK_W）无目的地走 DREAM_WALK_STEPS 步。
走出的起点—终点对若无既有边，就注册一条 origin="dream" 的可失败预测——
兑现/落空全部走 predictions 的标准结算（命中加粗+喜悦、超时痛感+好奇心燃料），
本模块自己不动情绪、不涨疲劳：梦里连的线只是猜想，世界说了才算数，绝不做统计固化。
"""
import random

from . import config
from .config import (DREAM_LOG_MAX, DREAM_PRIME_WINDOW, DREAM_WEAK_W,
                     DREAM_WALK_STEPS, PRED_HORIZON, PRED_MAX_PENDING)
from .state import append_note, log_event


def _primed_start(st):
    """从 priming 窗口内的最近激活概念里，按新近度加权随机挑一个梦游起点
    （Cai2009：没被 prime 过的概念进不了梦——REM 只放大已被点亮的远程联想）。
    返回概念名或 None（窗口内没有可梦的起点）。"""
    rounds = st.get("rounds", 0)
    latest = {}                                   # 概念 → 最近一次激活轮（去重保最新）
    for x in st.get("recent_active", []):
        c, r = x.get("c"), x.get("round", 0)
        if not c:
            continue
        age = rounds - r
        if age < 0 or age > DREAM_PRIME_WINDOW:   # 未来轮 / 超窗：不算被 prime
            continue
        if c not in latest or r > latest[c]:
            latest[c] = r
    if not latest:
        return None
    cons = list(latest)
    weights = [1.0 / (1.0 + max(0, rounds - latest[c])) for c in cons]   # 越新越容易入梦
    return random.choices(cons, weights=weights, k=1)[0]


def _weak_neighbors(rels, c):
    """概念 c 经弱边（w ≤ DREAM_WEAK_W）相连的邻居列表。"""
    out = []
    for k, v in rels.items():
        a, b = k.split("|")
        if a != c and b != c:
            continue
        other = b if a == c else a
        if other != c and v.get("w", 0) <= DREAM_WEAK_W:
            out.append(other)
    return out


def do_dream(st):
    """做一次梦：primed 起点 → 沿弱边随机走 → 起终点对下可失败预测。返回 (ok, detail)。
    全程不改情绪、不涨疲劳、不碰其他 state 键（只写 dream_log / edge_preds，
    笔记与 events 日志走公共工具；未成注只进 log，不写笔记）。"""
    start = _primed_start(st)
    if start is None:
        return False, "没有可梦的起点（最近没有激活的概念）"
    rels = st.get("relations", {})
    path = [start]
    for _ in range(DREAM_WALK_STEPS):
        cur, prev = path[-1], (path[-2] if len(path) >= 2 else None)
        nbrs = [n for n in _weak_neighbors(rels, cur) if n != prev]   # 不含刚来的概念
        if not nbrs:
            break                                   # 走不动（无弱边）就提前醒
        path.append(random.choice(nbrs))
    a, b = path[0], path[-1]
    preds = st.setdefault("edge_preds", [])
    bet = None
    if (a != b and "|".join(sorted([a, b])) not in rels
            and sum(1 for p in preds if p.get("status") == "pending") < PRED_MAX_PENDING):
        # dream 起源的注：兑现/落空走 predictions 的标准结算，这里不动情绪
        preds.append({"a": a, "b": b, "round": st["rounds"],
                      "deadline": st["rounds"] + PRED_HORIZON,
                      "status": "pending", "origin": "dream"})
        bet = [a, b]
    dlog = st.setdefault("dream_log", [])
    dlog.append({"round": st["rounds"], "path": path, "bet": bet})
    del dlog[:-DREAM_LOG_MAX]
    if bet:
        detail = f"梦游：{'→'.join(path)}…；下注 {a}↔{b}（{PRED_HORIZON}轮内同现则转正）"
        append_note(st, f"- 做梦：从《{a}》出发走过 {'→'.join(path)}，"
                        f"梦里把《{a}》和《{b}》连了一下——下了一注，等世界验证")
    else:
        detail = f"梦游：{'→'.join(path)}…；路径未成注"
    log_event(st, "dream", detail)
    return True, detail
