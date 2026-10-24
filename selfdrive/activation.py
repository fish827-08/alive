# -*- coding: utf-8 -*-
"""activation — v9.18 一层：并行扩散激活（spreading activation）。

理论：Collins & Loftus (1975) 扩散激活 + 全局工作空间"赢家通吃"（赢家浮上来
成为这一轮"它在想什么"）。一轮思考从起点并行撒 SA_PATHS 条联想路径，每条
最多 SA_MAX_STEPS 步；影响力分 = 边权 × 新鲜感^SA_NOVELTY_EXP × 情绪偏好，
最高者胜出并获赫布强化（+1），输家不动。
新鲜感指数是对 v9.15 同义反复（强边越赢越强、30/30 全命中）的对抗项；
SA_WIN_REPEAT_CAP 连赢上限再拦一道垄断。
跨层钩子（siblings_fn/abstract_of_fn 由二层 abstraction 提供）：路径可以
经由抽象节点（虚拟节点 ◆名）走到兄弟概念——底层和上层同时被点亮。"""
import random

from . import config
from .config import (ABSTRACT_LINK_SCORE, SA_MAX_STEPS, SA_NOVELTY_EXP,
                     SA_NOVELTY_NORM, SA_PATHS, SA_WIN_REPEAT_CAP)
from .state import clamp, record_active
from .tokenizer import FORMAT_NOISE


def _nbrs(rels, c):
    """概念 c 的邻接 {邻居: 边dict}。"""
    out = {}
    for k, v in rels.items():
        try:
            a, b = k.split("|")
        except ValueError:
            continue
        if a == c and b not in FORMAT_NOISE:
            out[b] = v
        elif b == c and a not in FORMAT_NOISE:
            out[a] = v
    return out


def _edge_score(st, edge, concept, cons):
    """边分 = w_norm × novelty^指数 × emo（好奇心高时偏爱低频概念）。"""
    w_norm = min(1.0, (edge.get("w", 0) or 0) / 10.0)
    age = st["rounds"] - edge.get("last_touch", st["rounds"])
    novelty = clamp(age / SA_NOVELTY_NORM, 0.05, 1.0)
    n_norm = min(1.0, (cons.get(concept, {}).get("n", 0) or 0) / 20.0)
    emo = 1.0 + 0.5 * st.get("emotions", {}).get("curiosity", 0.0) * (1.0 - n_norm)
    return w_norm * (novelty ** SA_NOVELTY_EXP) * emo


def _origin(st):
    """起点：语义中心首词（须是概念且非噪声）→ 度数最高的干净概念。"""
    cons = st.get("concepts", {})
    kw = st.get("last_kw", "")
    if kw and kw != "无":
        w = kw.split("、")[0]
        if w in cons and w not in FORMAT_NOISE:
            return w
    clean = [c for c in cons if c not in FORMAT_NOISE]
    if not clean:
        return None
    rels = st.get("relations", {})
    return max(clean, key=lambda c: sum(v.get("w", 0) for k, v in rels.items() if c in k.split("|")))


def spread(st, origin=None, n_paths=None, siblings_fn=None, abstract_of_fn=None):
    """并行撒 n_paths 条路径，返回按分降序的 [{"path","score","kind"}]。"""
    rels = st.get("relations", {})
    cons = st.get("concepts", {})
    if len(rels) < 3:
        return []
    start = origin or _origin(st)
    if not start or start in FORMAT_NOISE or not _nbrs(rels, start):
        return []
    n_paths = n_paths or SA_PATHS
    out = []
    for _ in range(n_paths):
        # 跨层钩子：0.6 概率经抽象节点走一条"抽象路径"
        if callable(abstract_of_fn) and callable(siblings_fn) and random.random() < 0.6:
            ab = abstract_of_fn(st, start)
            sibs = [s for s in siblings_fn(st, start) if s in cons] if ab else []
            if ab and sibs:
                sib = random.choice(sibs)
                edge = rels.get("|".join(sorted([start, sib])))
                base = _edge_score(st, edge, sib, cons) if edge else 0.3
                out.append({"path": [start, "◆" + ab, sib],
                            "score": base * ABSTRACT_LINK_SCORE, "kind": "abstract"})
                continue
        cur, score, path = start, 1.0, [start]
        for _step in range(SA_MAX_STEPS):
            nb = _nbrs(rels, cur)
            # 不立即回走（避免 A→B→A 把同一条边当两步念）
            cands_n = [c for c in nb if c != path[-2]] if len(path) >= 2 else list(nb)
            if not cands_n:
                break
            nxt = random.choice(cands_n)
            score *= _edge_score(st, nb[nxt], nxt, cons)
            path.append(nxt)
            cur = nxt
        if len(path) >= 2:
            out.append({"path": path, "score": score, "kind": "edge"})
    out.sort(key=lambda p: -p["score"])
    return out


def pick_thought(st, siblings_fn=None, abstract_of_fn=None):
    """一次完整思考：撒网 → 赢家通吃（防垄断）→ 赫布强化 → 记录+priming。"""
    cands = spread(st, siblings_fn=siblings_fn, abstract_of_fn=abstract_of_fn)
    if not cands:
        return None
    log = st.setdefault("sa_winner_log", [])
    tail = [w.get("path", [])[-1] for w in log[-SA_WIN_REPEAT_CAP:] if w.get("path")]
    winner = None
    for cand in cands:                      # 防垄断：连赢到顶的末端概念让位
        end = cand["path"][-1]
        if tail and len(tail) >= SA_WIN_REPEAT_CAP and len(set(tail)) == 1 and tail[-1] == end:
            continue
        winner = cand
        break
    if winner is None:
        winner = cands[0]
    rels = st.get("relations", {})
    real = [c for c in winner["path"] if not c.startswith("◆")]
    hebb_keys = []
    for i in range(len(real) - 1):          # 赫布：赢家路径相邻概念对 +1（同边去重）
        k = "|".join(sorted([real[i], real[i + 1]]))
        if k in rels and k not in hebb_keys:
            hebb_keys.append(k)
    for k in hebb_keys:
        rels[k]["w"] = (rels[k].get("w") or 0) + 1
        rels[k]["last_touch"] = st["rounds"]
    log.append({"round": st["rounds"], "path": winner["path"],
                "score": round(winner["score"], 4), "kind": winner["kind"]})
    del log[:-50]
    for c in real:
        record_active(st, c)                 # 三层 priming：白天点亮的，夜里可梦
    return {"winner": winner, "candidates": cands}


def hungry_pair(st, thought):
    """从赢家路径找一个无边概念对下注：(末端, 末端的二跳非邻接) 优先，(首,尾) 兜底。"""
    if not thought:
        return None
    rels = st.get("relations", {})
    cons = st.get("concepts", {})
    path = [c for c in thought["winner"]["path"] if not c.startswith("◆")]
    path = [c for c in path if c in cons and c not in FORMAT_NOISE]
    if len(path) < 2:
        return None
    end = path[-1]
    nb_end = set(_nbrs(rels, end))
    for first in _nbrs(rels, end):           # 末端邻居的邻居里找一个与末端无边的
        for second in _nbrs(rels, first):
            if second not in nb_end and second != end and second in cons and second not in FORMAT_NOISE:
                return (end, second) if random.random() < 0.5 else (second, end)
    k = "|".join(sorted([path[0], end]))
    if path[0] != end and k not in rels:
        return (path[0], end)
    return None
