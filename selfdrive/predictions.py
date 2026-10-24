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
from .config import (EVT_PRED_HIT, EVT_PRED_MISS, FUEL_PRED_MISS, FUEL_PRED_MISS_V2,
                     JOY_PRED_HIT, PAIN_PRED_MISS, PAIN_PRED_MISS_V2, PRED_HORIZON,
                     PRED_MAX_PENDING, PRED_SETTLE_PER_ROUND, REPLAY_QUEUE_MAX)
from .state import append_note, feel, log_event, mark


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


def verify_edge_predictions(st, present_words):
    """新文本的在场概念词集 → 核对在赌预测（v9.16.1 改为按词集判定：
    下注的两端都出现在同一段新文本里即兑现——比旧"top4 对集合"更容易公平结算）。
    兑现时顺把这对概念的边加粗 +1（青梧路径3：情绪塑造记忆权重）。"""
    if not present_words:
        return 0
    present = set(present_words)
    hits = 0
    for p in st.get("edge_preds", []):
        if p.get("status") != "pending":
            continue
        if p["a"] in present and p["b"] in present:
            p["status"] = "hit"
            p["settled_round"] = st["rounds"]
            hits += 1
            mark(st, EVT_PRED_HIT)
            feel(st, "satisfaction", JOY_PRED_HIT)
            rels = st.get("relations", {})
            rk = "|".join(sorted([p["a"], p["b"]]))
            r = rels.setdefault(rk, {"w": 0, "n": 0, "last_touch": st["rounds"]})
            r["w"] += 1          # 猜对的关联加粗：情绪真的改写知识图
            if p.get("origin") == "dream":
                r["type"] = "dream"   # 梦里连的边兑现了——标记出处
            # v9.19 类比接地：兑现经历给两端概念所属的抽象节点记一次"验证过"
            try:
                from .abstraction import bump_ground
                bump_ground(st, p["a"])
                bump_ground(st, p["b"])
            except Exception:
                pass
            append_note(st, f"- 预测兑现：我赌过《{p['a']}》和《{p['b']}》有关联，今天它们真的一起出现了（第 {p['round']} 轮下注，边已加粗）")
            log_event(st, "pred_settle", f"预测兑现：{p['a']}↔{p['b']}（第 {p['round']} 轮下注 → 第 {st['rounds']} 轮兑现，边+1）")
    return hits


def expire_edge_predictions(st):
    """超时未兑现 → 失误结算（痛感 + 好奇心燃料）。
    v9.16.1：有效期限 = min(记录的deadline, 下注轮+PRED_HORIZON)——老注自动缩短；
    单轮最多结算 PRED_SETTLE_PER_ROUND 笔，防一次性痛感洪水；每笔打 events 日志。"""
    misses = 0
    for p in st.get("edge_preds", []):
        if misses >= PRED_SETTLE_PER_ROUND:
            break
        if p.get("status") != "pending":
            continue
        eff_deadline = min(p.get("deadline", 0), p.get("round", 0) + PRED_HORIZON)
        if st["rounds"] > eff_deadline:
            p["status"] = "miss"
            p["settled_round"] = st["rounds"]
            misses += 1
            mark(st, EVT_PRED_MISS)
            # v9.19 错误闭环：落空要真的疼一下、困惑一下（旧版只记数字），
            # 并进错误重放队列，巩固期拿"对拍"复习——错的路会降权，对的路会加粗
            feel(st, "satisfaction", PAIN_PRED_MISS_V2)
            feel(st, "curiosity", FUEL_PRED_MISS_V2)
            rq = st.setdefault("replay_queue", [])
            rq.append({"kind": "pred_miss", "a": p["a"], "b": p["b"], "round": st["rounds"]})
            del rq[:max(0, len(rq) - REPLAY_QUEUE_MAX)]
            append_note(st, f"- 预测落空：我赌《{p['a']}》和《{p['b']}》有关联，{st['rounds'] - p['round']} 轮过去它们从未同现——这两个概念之间可能真的没关系，或者我还缺一座桥")
            log_event(st, "pred_settle", f"预测落空：{p['a']}↔{p['b']}（第 {p['round']} 轮下注 → 第 {st['rounds']} 轮落空）")
    return misses


def pred_stats(st):
    """命中率统计（诊断指标：目标不是 100%，是 20-50% 的可错区间）。"""
    settled = [p for p in st.get("edge_preds", []) if p.get("status") in ("hit", "miss")]
    if not settled:
        return "0/0"
    h = sum(1 for p in settled if p["status"] == "hit")
    return f"{h}/{len(settled)}"
