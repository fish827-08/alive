# -*- coding: utf-8 -*-
"""predictions — 可失败预测（v9.16 ②）。

诊断结论：旧 self_talk 从最强 20 条边里抽边、预测共同邻居——图里几乎必然成立，
30/30 全命中，零信息；强边反复加粗，弱边永远轮不到（自我强化闭环）。
新机制：只对【当前没有边】的概念对下注"A 和 B 有关联（会一起出现）"——
  - 之后读到的新文本里两者同现 → 命中（结构推断被世界兑现）
  - PRED_HORIZON 轮内从未同现 → 失误（下注失败，付一点痛感，收获一个谜）
预测必须可能错，错了才知道哪里有东西可学。
"""
import random

from . import config
from .config import (EVT_PRED_HIT, EVT_PRED_MISS, FUEL_PRED_MISS, JOY_PRED_HIT,
                     PAIN_PRED_MISS, PRED_HORIZON, PRED_MAX_PENDING)
from .state import append_note, feel, mark


def _adjacent_pairs(st):
    return {frozenset(k.split("|")) for k in st.get("relations", {})}


def make_edge_prediction(st):
    """对一对无边的概念下注。返回 (a, b) 或 None（概念不够/都在赌了）。"""
    preds = st.setdefault("edge_preds", [])
    pending = [p for p in preds if p.get("status") == "pending"]
    if len(pending) >= PRED_MAX_PENDING:
        return None
    cons = [c for c, v in st.get("concepts", {}).items()
            if v.get("n", 0) >= 2 and len(c) >= 2]
    if len(cons) < 6:
        return None
    adj = _adjacent_pairs(st)
    betting = {frozenset((p["a"], p["b"])) for p in pending}
    for _ in range(30):   # 最多试 30 次：找一对"相连概念多、彼此无边"的组合
        a = random.choice(cons)
        cands = []
        for b in cons:
            if b == a or a in b or b in a:
                continue
            pair = frozenset((a, b))
            if pair in adj or pair in betting:
                continue
            cands.append(b)
        if cands:
            b = random.choice(cands)
            preds.append({"a": a, "b": b, "round": st["rounds"],
                          "deadline": st["rounds"] + PRED_HORIZON, "status": "pending"})
            del preds[:-PRED_MAX_PENDING * 3]      # 保留已结算历史供统计
            return a, b
    return None


def verify_edge_predictions(st, cooccur_pairs):
    """新文本同现对 → 核对在赌预测。cooccur_pairs: {frozenset({a,b}), ...}"""
    if not cooccur_pairs:
        return 0
    hits = 0
    for p in st.get("edge_preds", []):
        if p.get("status") != "pending":
            continue
        if frozenset((p["a"], p["b"])) in cooccur_pairs:
            p["status"] = "hit"
            p["settled_round"] = st["rounds"]
            hits += 1
            mark(st, EVT_PRED_HIT)
            feel(st, "satisfaction", JOY_PRED_HIT)
            append_note(st, f"- 预测兑现：我赌过《{p['a']}》和《{p['b']}》有关联，今天它们真的一起出现了（第 {p['round']} 轮下注）")
    return hits


def expire_edge_predictions(st):
    """超时未兑现 → 失误结算（痛感 + 好奇心燃料：猜错的地方有东西可学）。"""
    misses = 0
    for p in st.get("edge_preds", []):
        if p.get("status") == "pending" and st["rounds"] > p.get("deadline", 0):
            p["status"] = "miss"
            p["settled_round"] = st["rounds"]
            misses += 1
            mark(st, EVT_PRED_MISS)
            feel(st, "satisfaction", PAIN_PRED_MISS)
            feel(st, "curiosity", FUEL_PRED_MISS)
            append_note(st, f"- 预测落空：我赌《{p['a']}》和《{p['b']}》有关联，{PRED_HORIZON} 轮过去它们从未同现——这两个概念之间可能真的没关系，或者我还缺一座桥")
    return misses


def pred_stats(st):
    """命中率统计（诊断指标：目标不是 100%，是 20-50% 的可错区间）。"""
    settled = [p for p in st.get("edge_preds", []) if p.get("status") in ("hit", "miss")]
    if not settled:
        return "0/0"
    h = sum(1 for p in settled if p["status"] == "hit")
    return f"{h}/{len(settled)}"
