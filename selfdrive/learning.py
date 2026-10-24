# -*- coding: utf-8 -*-
"""learning — 关系学习：概念晋升 / 赫布建边 / 规则归纳 / 变换识别 / 概念合并（v9.16 重写）。

v9.16 ⑤ 概念晋升制（替代"见词即概念"）：
  - 新词先进候选池 word_cands（记出现次数与来源数）
  - 晋升条件：总次数 ≥3 且来源 ≥2（跨文本复现）｜或 ≥5 次（单文本高频术语）
    ｜或来自文件标题/问题编号（人工策划的干净入口）
  - 晋升与计数时先做碎片归并：短词若是已有更长概念的子串，计数直接记给长词
    ——"关系的本"不会再作为概念出生，它一出现就并入"关系的本质"
  - 词汇靠使用挣来，不再靠人喂词表（那是 noisy_words 走过的老路）
边/规则只在"已晋升概念"之间建立；大脑回答的关系类型（相反/先后）可随问题标记打入。
"""
import re
from collections import Counter
from pathlib import Path

from . import predictions
from . import tokenizer
from .state import append_note, record_active

_NUM = "一二三四五六七八九十"
PROMOTE_MIN_N = 3        # 跨源晋升：总次数下限
PROMOTE_MIN_SRCS = 2     # 跨源晋升：来源数下限
PROMOTE_HOT_N = 5        # 单源高频术语直接晋升
CAND_KEEP = 800          # 候选池容量（LRU 截断）


def _absorb_into_longer(word, pool):
    """碎片归并：word 是 pool 中更长词条的【前缀】，或 ≥4 字子串 → 返回长词。
    注意子串≠碎片："反馈"是"负反馈"的后缀但两者是不同概念——后缀短词不吸收；
    前缀碎片（关系的本→关系的本质…）和长子串（质与网络≥4字）才是切分残渣。"""
    best = None
    for w in pool:
        if w == word:
            continue
        if w.startswith(word) or (len(word) >= 4 and word in w):
            if best is None or len(w) > len(best):
                best = w
    return best


def bump_word(st, word, source):
    """候选词计数 + 晋升判定。返回晋升后的正式概念名（未晋升返回 None）。
    碎片归并：先查正式概念与候选池里有没有更长词条包含它。"""
    cons = st.setdefault("concepts", {})
    cands = st.setdefault("word_cands", {})

    host = _absorb_into_longer(word, cons) or _absorb_into_longer(word, cands)
    if host and host != word:
        word = host        # "关系的本" → 记给"关系的本质"

    if word in cons:
        cons[word]["n"] = (cons[word].get("n") or 0) + 1
        cons[word]["last"] = st["rounds"]
        return word

    c = cands.setdefault(word, {"n": 0, "srcs": [], "first": st["rounds"], "last": 0})
    c["n"] += 1
    c["last"] = st["rounds"]
    if source not in c["srcs"]:
        c["srcs"].append(source)
        del c["srcs"][5:]     # 最多记 5 个来源（删第 5 个之后，不是之前——之前写反了）
    if (c["n"] >= PROMOTE_MIN_N and len(c["srcs"]) >= PROMOTE_MIN_SRCS) or c["n"] >= PROMOTE_HOT_N:
        del cands[word]
        cons[word] = {"n": c["n"], "last": st["rounds"]}
        # 晋升时反向吸收：候选池里新概念的碎片（前缀/长子串）全部并进来
        for frag in [w for w in cands if w != word and (word.startswith(w) or (len(w) >= 4 and w in word))]:
            cons[word]["n"] += cands[frag]["n"]
            del cands[frag]
        # 正式概念里若有新词的碎片子串（旧数据残留）→ 合并
        for frag in [w for w in cons if w != word and (word.startswith(w) or (len(w) >= 4 and w in word))]:
            merge_concepts(st, frag, word)
            append_note(st, f"- 碎片合并：『{frag}』并入了『{word}』（边权已转移）")
        return word
    if len(cands) > CAND_KEEP:
        for old in sorted(cands, key=lambda k: cands[k].get("last", 0))[:-CAND_KEEP]:
            del cands[old]
    return None


def promote_direct(st, word, max_len=8):
    """策划入口直接晋升（文件标题/大脑确认过的概念；标题放宽到 12 字）。"""
    if not word or not (2 <= len(word) <= max_len):
        return None
    word = _absorb_into_longer(word, st.get("concepts", {})) or \
        _absorb_into_longer(word, st.get("word_cands", {})) or word
    cons = st.setdefault("concepts", {})
    cc = cons.setdefault(word, {"n": 0, "last": 0})
    cc["n"] = (cc.get("n") or 0) + 1
    cc["last"] = st["rounds"]
    st.get("word_cands", {}).pop(word, None)
    return word


def merge_concepts(st, drop, keep):
    """把概念 drop 并入 keep：计数相加、关系边改写（权重合并）、层级同步。
    [RULE] 合并:X→Y 与自动碎片合并共用这一条（老师纠错 → 结构变化，而非黑名单）。"""
    if not drop or not keep or drop == keep:
        return False
    cons = st.get("concepts", {})
    cands = st.get("word_cands", {})
    cc = cons.setdefault(keep, {"n": 0, "last": 0})
    if drop in cons:
        cc["n"] = (cc.get("n") or 0) + cons[drop].get("n", 0)
        cc["last"] = max(cc.get("last", 0), cons[drop].get("last", 0))
        del cons[drop]
    if drop in cands:                    # 候选池里的计数也一并转移
        cc["n"] = (cc.get("n") or 0) + cands[drop].get("n", 0)
        cc["last"] = max(cc.get("last", 0), cands[drop].get("last", 0))
        del cands[drop]
    else:
        cands.pop(drop, None)
    rels = st.get("relations", {})
    for k in list(rels.keys()):
        try:
            a, b = k.split("|")
        except ValueError:
            continue
        if drop not in (a, b):
            continue
        na = keep if a == drop else a
        nb = keep if b == drop else b
        old = rels[k]
        if na == nb:
            del rels[k]
            continue
        nk = "|".join(sorted([na, nb]))
        if nk in rels and nk != k:
            rels[nk]["w"] = (rels[nk].get("w") or 0) + (old.get("w") or 0)
            rels[nk]["n"] = (rels[nk].get("n") or 0) + (old.get("n") or 0)
            rels[nk]["last_touch"] = max(rels[nk].get("last_touch", 0), old.get("last_touch", 0))
            del rels[k]
        else:
            if nk != k:
                rels[nk] = rels.pop(k)
    hier = st.get("hierarchy", {})
    for parent in list(hier):
        hier[parent] = [keep if x == drop else x for x in hier[parent] if x != keep]
    hier.pop(drop, None)
    return True


def _bump_transforms(st, text):
    """v9.11 数字加法句式识别（owner 授权 2026-10-23）：X加Y…等于Z →
    表达式/操作数/结果全部直接晋升（数字词是干净封闭集）+ 变换边 + 结构边。"""
    cons = st.setdefault("concepts", {})
    rels = st.setdefault("relations", {})
    found = 0
    for m in re.finditer(r"([%s]+(?:加[%s]+)+)等于([%s]+)" % (_NUM, _NUM, _NUM), text):
        expr, res = m.group(1), m.group(2)
        ops = re.findall(r"[%s]+" % _NUM, expr)
        for w in [res] + ops + [expr]:
            cc = cons.setdefault(w, {"n": 0, "last": 0})
            cc["n"] += 1
            cc["last"] = st["rounds"]
        tfs = st.setdefault("transforms", {})
        tkey = expr + "→" + res
        tf = tfs.setdefault(tkey, {"operands": ops, "result": res, "support": 0, "last": 0})
        tf["support"] += 1
        tf["last"] = st["rounds"]
        for x, y in ((expr, res),):
            rkey = "|".join(sorted([x, y]))
            r = rels.setdefault(rkey, {"w": 0, "n": 0})
            r["w"] += 2
            r["n"] += 1
            r["last_touch"] = st["rounds"]
        for o in ops:
            rk = "|".join(sorted([expr, o]))
            ro = rels.setdefault(rk, {"w": 0, "n": 0})
            ro["w"] += 1
            ro["n"] += 1
            ro["last_touch"] = st["rounds"]
        found += 1
    return found


def learn_from_text(st, text, title, source, rel_type=None):
    """读文本 → 概念晋升 + 同现建边（Hebb）+ 规则归纳 + 预测核对。
    rel_type：该文本关系边的默认类型（相反/先后问题的回答 → contrast/temporal）。
    返回 (晋升/触及概念数, 关系边总数, 规则总数)。"""
    if not text or len(text) < 20:
        return 0, len(st.get("relations", {})), len(st.get("rules", {}))
    wc = tokenizer.concept_words(text[:600])
    if not wc:
        return 0, len(st.get("relations", {})), len(st.get("rules", {}))

    _bump_transforms(st, text[:600])

    # 候选晋升：文本前 15 词逐一记数；标题概念词直接晋升（策划入口）
    promoted_now = []
    for w, _n in wc.most_common(15):
        if w == title:
            continue
        p = bump_word(st, w, source)
        if p:
            promoted_now.append(p)
    title_concept = None
    if title:
        # 标题先过器官取真词（拒绝 QID/web 时间戳这类结构串——v9.8"大脑回答→QID"噪音规则的病源）
        tw = tokenizer.concept_words(title)
        if tw:
            title_concept = promote_direct(st, tw.most_common(1)[0][0], max_len=12)
            if title_concept:
                promoted_now.append(title_concept)

    cons = st.setdefault("concepts", {})
    rels = st.setdefault("relations", {})
    cands = st.get("word_cands", {})
    # 建边只在"正式概念 ∩ 本文高频词"之间（碎片不再生边）；
    # v9.16.1: 相反/先后类回答（rel_type）放宽到 n≥2 候选词——提问和建图不再断链
    # （title_concept 参与建边，只在规则前提里被排除——旧排除把《信息论》类标题词
    #   挡在边外，正是 contrast/temporal 长不出来的原因之一）
    if rel_type:
        present = [w for w, _ in wc.most_common(12)
                   if w in cons or cands.get(w, {}).get("n", 0) >= 2]
    else:
        present = [w for w, _ in wc.most_common(12) if w in cons]
    top = present[:4]
    for w in top:                          # v9.18 三层 priming：读到的概念先点亮，夜里可梦
        record_active(st, w)
    pairs = set()
    for i in range(min(3, len(top))):
        for j in range(i + 1, min(4, len(top))):
            pairs.add(frozenset((top[i], top[j])))
    for pair in pairs:
        a, b = sorted(pair)
        key = "|".join([a, b])
        r = rels.setdefault(key, {"w": 0, "n": 0})
        r["w"] += 1
        r["n"] += 1
        r["last_touch"] = st["rounds"]
        if rel_type and not r.get("type"):
            r["type"] = rel_type
    # ② 预测核对：下注两端都出现在这段新文本的在场词集里 = 兑现（顺带边加粗）
    predictions.verify_edge_predictions(st, present)
    # 规则归纳 A∧B→标题（标题已是正式概念）
    if len(top) >= 2 and title_concept and title_concept not in top[:2]:
        pre = sorted([top[0], top[1]])
        rkey = "∧".join(pre) + "→" + title_concept
        rr = st.setdefault("rules", {}).setdefault(rkey, {
            "premises": pre, "conclusion": title_concept, "support": 0,
            "confidence": 0.5, "status": "pending", "first": st["rounds"]})
        rr["support"] += 1
    return len(promoted_now), len(rels), len(st.get("rules", {}))


def sweep_legacy_fragments(st, limit=3):
    """渐进清理旧 state 的碎片概念：每轮最多合并 limit 对
    （短词是长词的前缀，或 ≥4 字子串——与 bump_word 同一条碎片规则）。
    旧库 21% 碎片不可能一夜清完，也不该一夜清完——慢慢并，笔记里每条都有记录。"""
    cons = st.get("concepts", {})
    merged = 0
    for short in list(cons.keys()):
        if merged >= limit:
            break
        for long_w in cons:
            if short == long_w:
                continue
            if long_w.startswith(short) or (len(short) >= 4 and short in long_w):
                if merge_concepts(st, short, long_w):
                    append_note(st, f"- 遗产碎片合并：『{short}』并入『{long_w}』（剩余概念 {len(cons)} 个）")
                    merged += 1
                break
        if merged >= limit:
            break
    return merged


def prune_concepts(st):
    """记忆巩固期的概念/候选修剪：单次出现、长期无人使用、不进任何规则的 → 退场。
    词汇靠使用挣来——也要靠不使用失去。"""
    cons = st.get("concepts", {})
    rules = st.get("rules", {})
    used = set()
    for v in rules.values():
        used.update(v.get("premises", []))
        used.add(v.get("conclusion", ""))
    tfs = st.get("transforms", {})
    for v in tfs.values():
        used.update(v.get("operands", []))
        used.add(v.get("result", ""))
    removed = 0
    for w in list(cons.keys()):
        v = cons[w]
        if v.get("n", 0) <= 1 and w not in used \
                and (st["rounds"] - v.get("last", st["rounds"])) > 400 and len(w) < 8:
            del cons[w]
            removed += 1
    if removed:
        rels = st.get("relations", {})
        for k in list(rels.keys()):
            a, b = k.split("|")
            if a not in cons or b not in cons:
                del rels[k]
        hier = st.get("hierarchy", {})
        for parent in list(hier):
            hier[parent] = [x for x in hier[parent] if x in cons]
            if not hier[parent] or parent not in cons:
                hier.pop(parent, None)
    cands = st.get("word_cands", {})
    for w in list(cands.keys()):
        if cands[w].get("n", 0) <= 1 and (st["rounds"] - cands[w].get("last", 0)) > 300:
            del cands[w]
    return removed


def infer_from_graph(st, topic):
    """推理通道（v9.7）：激活扩散 A+C→未知 D，请大脑判定。沿用 v9.12 噪声过滤、
    v9.14 错误记忆回避。返回 (A, C, D, strength) 或 None。"""
    from .tokenizer import FORMAT_NOISE
    if not topic or topic in FORMAT_NOISE:
        return None
    rels = st.get("relations", {})
    if len(rels) < 3:
        return None
    _bad_pairs = {tuple(m.get("pattern", [])) for m in st.get("mistakes", []) if m.get("pattern")}

    def nbrs(c):
        out = {}
        for k, v in rels.items():
            a, b = k.split("|")
            if b in FORMAT_NOISE or a in FORMAT_NOISE:
                continue
            if a == c:
                out[b] = out.get(b, 0) + v["w"]
            elif b == c:
                out[a] = out.get(a, 0) + v["w"]
        return out

    nA = nbrs(topic)
    if not nA:
        return None
    A = topic
    cand = sorted(nA.items(), key=lambda x: -x[1])
    if len(cand) < 2:
        return None
    C = None
    for _cc, _ in cand:
        if tuple(sorted([A, _cc])) not in _bad_pairs:
            C = _cc
            break
    if C is None:
        return None
    nC = nbrs(C)
    best, best_s = None, 0
    for d, wa in nA.items():
        if d in (A, C):
            continue
        s = wa + nC.get(d, 0)
        if s > best_s:
            best_s, best = s, d
    if best is None:
        for d, wc in nC.items():
            if d in (A, C):
                continue
            if wc > best_s:
                best_s, best = wc, d
    if best is None or best_s <= 0:
        return None
    return A, C, best, best_s


def transform_compose(st):
    """v9.12 组合泛化：把已知 transforms 当改写规则，对未见表达式做符号归约。
    不内置算术——结果必须由已知规则链归约得到。返回 (expr, result, via) 或 None。"""
    tfs = st.get("transforms", {})
    if len(tfs) < 2:
        return None
    direct = {}
    for v in tfs.values():
        ops = tuple(v.get("operands", []))
        if ops and v.get("result"):
            direct[tuple(sorted(ops))] = v["result"]
    expand = {}
    for ops, res in direct.items():
        if res and len(ops) == 2 and res not in expand:
            expand[res] = ops

    def atoms(x, depth=0):
        if depth > 6 or x not in expand:
            return (x,)
        out = []
        for sub in expand[x]:
            out.extend(atoms(sub, depth + 1))
        return tuple(out)

    for a in sorted(expand.keys()):
        for b in sorted(expand.keys()):
            ops_key = tuple(sorted([a, b]))
            if ops_key in direct:
                continue
            flat = tuple(sorted(list(atoms(a)) + list(atoms(b))))
            if flat in direct:
                expr = a + "加" + b
                via = (f"{a}={'加'.join(expand[a])}、{b}={'加'.join(expand[b])}，"
                       f"合起来是{'加'.join(flat)}")
                return expr, direct[flat], via
    return None
