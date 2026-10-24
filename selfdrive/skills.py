# -*- coding: utf-8 -*-
"""skills — 技能实现（v9.16 拆出；13 个技能的 do_* 函数与扫描/阅读工具）。

v9.16 行为改造落点：
  do_explore_file    ① web 配额过滤（超限不再选 web 文件）+ 读自缓存标记/痛感
  do_ask_brain       ④ 查重拦截（重复问题不许问，付痛感）+ 相反/先后问题打关系类型
  do_read_brain      ③ [RULE] 编译接入（替代 v9.15.2 的自动学噪音钩子——后者把
                     "关系"拉黑过，教训：批评要进决策层，不能进停用词表）
  do_self_talk       ② 改为"无边概念对下注"（旧共同邻居预测 30/30 全命中，零信息）
  do_consolidate     ⑤ 概念/候选修剪 + ② 预测超时结算
"""
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from . import config, learning, predictions, rules_compiler, templates, tokenizer

try:                                    # 顶层模块（BASE/semantics.py，兜底联想层）
    import semantics as sem
except ImportError:
    sys.path.insert(0, str(config.BASE))
    import semantics as sem
from .config import (ARCHIVE, BASE, BLACKLIST_DIRS, BRAIN, INBOX, LOGS, NOTES,
                     OUTBOX, SEARCH_BLACK_WORDS, SEED_TEMPLATES,
                     WEB, WIDER_EXTS, WIDER_ROOTS, load_ask_config)
from .decision import explore_starved
from .kb import kb_remember
from .state import (append_note, feel, mark, push_read, web_share)
from .tokenizer import FORMAT_NOISE
from .learning import infer_from_graph, learn_from_text, transform_compose


# ---------- 扫描与阅读工具 ----------

def scan_workspace():
    """同时扫描 archive 与 web（搜索结果沉淀后可回读——但受①配额约束）。"""
    files, dirs = [], []
    for root in (ARCHIVE, WEB):
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file():
                    files.append({"path": str(p.relative_to(BASE)), "name": p.name,
                                  "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                                  "ext": p.suffix.lower()})
                elif p.is_dir():
                    dirs.append(str(p.relative_to(BASE)))
    return files, dirs


def scan_wider(st):
    found = []
    for root in WIDER_ROOTS:
        rp = Path(root)
        if not rp.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(rp):
            dirnames[:] = [d for d in dirnames if d not in BLACKLIST_DIRS and not d.startswith(".")]
            if str(BASE) in dirpath or dirpath.startswith(str(BASE)):
                continue
            for fn in filenames[:200]:
                p = Path(dirpath) / fn
                if p.suffix.lower() in WIDER_EXTS and p.stat().st_size < 200_000:
                    key = str(p)
                    if key not in st["wider_files"]:
                        st["wider_files"][key] = time.time()
                        found.append(key)
    return found


def novelty_score(files, st):
    if not files:
        return 0.0
    total = len(files)
    newish = sum(1 for f in files if time.time() - f["mtime"] < 7 * 86400)
    unseen = sum(1 for f in files if f["path"] not in st["visited_files"])
    return 0.5 * (newish / total) + 0.5 * (unseen / total)


def read_head(path, n=20):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return [l.rstrip() for l in f.readlines()[:n]]
    except Exception:
        return []


def read_full(path, max_chars=sem.MAX_CHARS):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read(max_chars)
    except Exception:
        return ""


def semantic_note(full_text, path, top, kind="text"):
    """全文 → kb 记忆库存放（失败静默降级本地笔记）。"""
    tags = [t for t in top.split("、") if t][:6]
    kb_ok = kb_remember(full_text, tags, str(path))
    relate = f"记忆已存放（{'kb' if kb_ok else 'kb暂不可用，本地笔记'})"
    note = f"\n- 语义记忆：{relate}"
    return note, relate


# ---------- 技能：探索 ----------

def do_explore_file(files, st):
    unseen = [f for f in files if f["path"] not in st["visited_files"]]
    pool = unseen if unseen else files
    # ① v9.16 世界重开：web 自食缓存占比超限 → 本轮不选 web 文件
    over_quota = web_share(st) >= config.WEB_SHARE_CAP
    if over_quota:
        non_web = [f for f in pool if "web-" not in f["path"]]
        if non_web:
            pool = non_web
        elif unseen:
            return False, (f"web 缓存占比 {web_share(st):.0%} 已超 {config.WEB_SHARE_CAP:.0%} 配额，"
                           f"非 web 文件都已读完——去搜索/求知找新口粮，不吃自己的缓存")
    if not pool:
        return False, "无文件可读"
    f = random.choice(pool)
    is_web = "web-" in f["path"]
    if is_web:
        push_read(st, "web")
        st["last_read_kind"] = "web"
        mark(st, config.EVT_READ_CACHE)          # ④ 读自缓存：可被 [RULE]/痛感钩住
    else:
        push_read(st, "arch")
        st["last_read_kind"] = "arch"

    full = read_full(BASE / f["path"])
    lines = full.splitlines()
    title = lines[0][:60] if lines else f["name"]
    words = tokenizer.concept_words(" ".join(lines[:40]))
    top = "、".join(w for w, _ in words.most_common(8)) or "无"
    note, relate = semantic_note(full, BASE / f["path"], top)
    st["last_kw"] = top

    # v9.8c 预测机制：读前按文件名×已有概念生成预期，读后核对，误差进记忆
    _pc = load_ask_config().get("predict", {})
    _pk = int(_pc.get("topk", 3))
    _concepts = set(st.get("concepts", {}).keys())
    _expect = []
    for _w in re.findall(r"[\u4e00-\u9fa5]{2,}|\w{4,}", f["name"]):
        for _c in _concepts:
            if _c in _w or _w in _c:
                _expect.append(_c)
    _expect = list(dict.fromkeys(_expect))[:_pk]
    _actual = [w for w, _ in words.most_common(8)]
    _hit = [c for c in _expect if c in _actual]
    _miss = [c for c in _expect if c not in _actual]
    _surprise = [w for w in _actual if w not in _expect and w in _concepts][:3]
    # 分层预测粗猜（v9.15 保留）
    _fn = f["name"]
    _coarse_guess = []
    if any(n in _fn for n in ("数", "加", "算", "字", "后继", "概率")):
        _coarse_guess.append("数字/加法")
    if any(w in _fn for w in ("天", "温", "风", "天气")):
        _coarse_guess.append("天气")
    if any(l in _fn for l in ("信号", "语言", "符号", "人工")):
        _coarse_guess.append("语言/信号")
    if not _coarse_guess:
        _coarse_guess.append("其他")
    _coarse_hit = any(
        (c == "数字/加法" and re.search(r"[一二三四五六七八九十加等于]", " ".join(_actual)) is not None or
         c == "天气" and any(w in " ".join(_actual) for w in ("温度", "天", "风")) or
         c == "语言/信号" and any(w in " ".join(_actual) for w in ("信号", "语言", "符号")))
        for c in _coarse_guess)
    _pl = st.setdefault("pred_log", [])
    _pl.append({"round": st["rounds"], "file": f["name"], "expect": _expect,
                "hit": _hit, "miss": _miss, "surprise": _surprise})
    del _pl[:max(0, len(_pl) - int(_pc.get("log_size", 50)))]
    if _expect:
        append_note(st, f"预测核对：预期{','.join(_expect) or '无'}｜命中{','.join(_hit) or '无'}｜未兑现{','.join(_miss) or '无'}｜意外{','.join(_surprise) or '无'}｜粗猜类别{','.join(_coarse_guess)}{'命中' if _coarse_hit else '未命中'}")
    st.setdefault("pred_layers", []).append({
        "round": st["rounds"], "file": f["name"],
        "coarse": _coarse_guess, "coarse_hit": _coarse_hit,
        "fine_expect": _expect, "fine_surprise": _surprise})
    del st["pred_layers"][-50:]
    # 注意过滤行为化（v9.9）：web 文件 surprise 全为格式词 → 低价值跳过学习
    _low_value = is_web and _surprise and all(w in FORMAT_NOISE for w in _surprise)
    _pl[-1]["low_value"] = _low_value
    # LP 学习进展（v9.14）
    _lp_map = st.setdefault("lp", {})
    _topic_lp = (st.get("last_kw") or "").split("、")[0] or f["name"]
    _err = len(_surprise) / max(1, len(_actual))
    _rec = _lp_map.setdefault(_topic_lp, {"err_history": [], "lp": 0.0, "last_round": 0})
    _rec["err_history"].append(round(_err, 3))
    del _rec["err_history"][:-4]
    _h = _rec["err_history"]
    if len(_h) >= 2:
        _k = max(1, len(_h) // 2)
        _rec["lp"] = round(sum(_h[:_k]) / _k - sum(_h[_k:]) / len(_h[_k:]), 3)
    _rec["last_round"] = st["rounds"]
    if len(_lp_map) > 20:
        for _oldk in sorted(_lp_map, key=lambda k: _lp_map[k]["last_round"])[:-20]:
            del _lp_map[_oldk]
    if _low_value:
        append_note(st, f"低价值跳过学习：{f['name']}（surprise全为格式噪声：{','.join(_surprise)}）")
    else:
        learn_from_text(st, full, Path(f["name"]).stem, f["name"])
    quota_sfx = f"，web占比{web_share(st):.0%}/{config.WEB_SHARE_CAP:.0%}" if is_web else ""
    append_note(st, f"## 读到：{f['name']}\n- 首行：{title}\n- 关键词：{top}{note}")
    if f["path"] not in st["visited_files"]:
        st["visited_files"].append(f["path"])
    return True, f"读了 {f['name']}（{len(lines)} 行，关键词：{top}，{relate}{quota_sfx}）"


def do_explore_wider(st):
    found = scan_wider(st)
    if not found:
        return False, "更大范围没有新文本文件"
    key = random.choice(found)
    try:
        full = read_full(key)
        lines = full.splitlines()
        words = tokenizer.concept_words(" ".join(lines[:40]))
        top = "、".join(w for w, _ in words.most_common(8)) or "无"
        note, relate = semantic_note(full, key, top)
        st["last_kw"] = top
        push_read(st, "wider")
        st["last_read_kind"] = "wider"
        learn_from_text(st, full, Path(key).stem, key)
        append_note(st, f"## 探到外部文件：{key}\n- 首行：{lines[0][:60] if lines else ''}\n- 关键词：{top}{note}")
        return True, f"读了外部文件 {Path(key).name}（{len(lines)} 行，关键词：{top}，{relate}）"
    except Exception as exc:
        return False, f"读取失败：{exc}"


def do_visit_new_dir(dirs, st):
    unseen = [d for d in dirs if d not in st["visited_dirs"]]
    if not unseen:
        return False, "目录都看过了"
    d = random.choice(unseen)
    p = BASE / d
    names = [x.name for x in p.iterdir() if not x.name.startswith(".")][:8]
    append_note(st, f"## 探访目录：{d}\n- 发现：{len(names)} 个条目：{'、'.join(names[:5])}")
    st["visited_dirs"].append(d)
    return True, f"探访新目录 {d}（发现 {len(names)} 个条目）"


def do_tidy_notes(st):
    NOTES.mkdir(parents=True, exist_ok=True)
    notes_file = NOTES / "journey.md"
    if notes_file.exists():
        text = notes_file.read_text(encoding="utf-8")
        words = tokenizer.concept_words(text)
        top = "、".join(w for w, _ in words.most_common(10))
    else:
        top = "暂无笔记"
    with open(NOTES / "index.md", "w", encoding="utf-8") as f:
        f.write(f"# 探索索引（第 {st['rounds']} 轮）\n高频词：{top}\n已读 {len(st['visited_files'])} 个文件\n")
    removed = templates.prune_templates(st)
    extra = f"，模板库淘汰 {removed} 个低频模板" if removed else ""
    return True, f"整理笔记（高频词：{top[:40]}）{extra}"


# ---------- 技能：问答 ----------

def _ask_topic(st):
    """提问主题：语义中心首个词（≤12字），回退文件名。"""
    kw = st.get("last_kw", "")
    if kw and kw != "无":
        t = kw.split("、")[0][:12]
        if t:
            return t
    if st["visited_files"]:
        return Path(st["visited_files"][-1]).name
    return ""


def _self_guess(st, topic):
    """自问自答（青梧路径5）：提问前从自己的概念网押一个"答案会提到的概念"。"""
    if not topic:
        return None
    rels = st.get("relations", {})
    best, bw = None, 0
    for k, v in rels.items():
        a, b = k.split("|")
        if a == topic and v.get("w", 0) > bw:
            best, bw = b, v.get("w", 0)
        elif b == topic and v.get("w", 0) > bw:
            best, bw = a, v.get("w", 0)
    return best


def _write_question(st, qid, question, source, rel_tag=None, topic=""):
    BRAIN.mkdir(parents=True, exist_ok=True)
    with open(INBOX, "a", encoding="utf-8") as f:
        f.write(f"[{qid}] {question}\n")
    st["brain_questions"].append(qid)
    st["last_ask_round"] = st["rounds"]
    templates.remember_ask(st, question)
    mark(st, config.EVT_ASK)
    if rel_tag:
        # 相反/先后问题附带主题词（v9.16.1 全量：读回答时按句式收割 typed 边）
        st.setdefault("q_rel_tags", {})[qid] = ({"type": rel_tag, "topic": topic}
                                                if topic else rel_tag)
    guess = _self_guess(st, topic)
    if guess:
        st.setdefault("q_self_guess", {})[qid] = {"guess": guess, "round": st["rounds"]}
    return True, f"已向大脑提问 [{qid}]（来自{source}）：{question}"


def do_ask_brain(st):
    """写一个问题到 inbox（节流 + 未读抑制 + v9.16 查重拦截④）。
    通道优先级：变换组合 → 图激活推理 → 开放求知 → 模板（相反/先后内置其中）。"""
    if st["rounds"] - st["last_ask_round"] < 3:
        return False, "提问节流中（距上次提问不足 3 轮）"
    OUTBOX.mkdir(parents=True, exist_ok=True)
    unread = [p for p in OUTBOX.glob("*.md") if p.stem not in st["brain_read"]]
    if unread:
        return False, "已有未读回答，先读再问"
    qid = time.strftime("Q-%Y%m%d-%H%M")
    if qid in st["brain_questions"]:
        return False, "本分钟已提问"
    topic = _ask_topic(st)
    candidates = []   # {"q","source","rel_tag","kind","meta"}

    # 通道 A：变换组合猜想（演绎→验证→习得）
    if (st["rounds"] - st.get("last_infer_round", -999)) >= 20:
        tc = transform_compose(st)
        if tc:
            expr_t, result_t, via_t = tc
            candidates.append({
                "q": (f"我用记忆里的加法变换做了一次组合推导：{via_t}，"
                      f"所以我推出『{expr_t}等于{result_t}』——这是我推理出的未知联系。"
                      f"请判断这个推理对不对（请在回答开头写 [VERDICT:真] 或 [VERDICT:假] 或 [VERDICT:存疑]）。"),
                "source": f"变换组合通道（{expr_t}→{result_t}）", "rel_tag": None,
                "kind": "transform", "meta": (expr_t, result_t)})
    # 通道 B：概念网激活扩散 A+C→未知 D
    if (st["rounds"] - st.get("last_infer_round", -999)) >= 20 and topic:
        inf = infer_from_graph(st, topic)
        if inf:
            A, C, D, strength = inf
            candidates.append({
                "q": (f"我最近读的《{A}》让我联想到《{C}》，在我的记忆里两者都和《{D}》相连。"
                      f"我推断《{A}》和《{C}》一起出现时，可能会涉及《{D}》——这是我推理出的未知联系。"
                      f"请判断这个推理对不对（请在回答开头写 [VERDICT:真] 或 [VERDICT:假] 或 [VERDICT:存疑]）。"),
                "source": f"推理通道（{A}+{C}→{D}，强度{strength}）", "rel_tag": None,
                "kind": "infer", "meta": (A, C, D)})
    # 通道 C：开放求知
    starved = explore_starved(st)
    hungry = (st["rounds"] - st["last_ask_round"]) >= 6
    roll = random.random()
    oa = load_ask_config().get("open_ask", {})
    go_open = False
    last_open = st.get("last_open_ask_round", -999)
    if (st["rounds"] - last_open) >= oa.get("cooldown_rounds", 0):
        if starved and oa.get("starved_force", True):
            go_open = True
        elif starved:
            go_open = roll < oa.get("starved_prob", 1.0)
        elif hungry and oa.get("hungry_force", False):
            go_open = True
        elif hungry:
            go_open = roll < oa.get("hungry_prob", 0.5)
        else:
            go_open = roll < oa.get("random_prob", 0.35)
    if go_open:
        streak = 1 if (st["rounds"] - last_open) > 12 else st.get("open_streak", 0) + 1
        st["open_streak"] = streak
        if streak > oa.get("max_consecutive", 99):
            go_open = False
        else:
            st["last_open_ask_round"] = st["rounds"]
            st["open_asks"] = st.get("open_asks", 0) + 1
            if topic:
                q = (f"我想学点新东西：请讲一件我记忆库里没有的新知识，"
                     f"最好和我最近读的《{topic}》有关，或能帮我理解它的反面或盲区。")
            else:
                q = "我想学点新东西：请讲一件我记忆库里没有的新知识，并说说它为什么值得了解。"
            candidates.append({"q": q, "source": f"开放求知（starved={starved},roll={roll:.2f}）",
                               "rel_tag": None, "kind": "open", "meta": None})
    # 通道 D：模板（含内置相反/先后）——生成多个备选供查重轮换
    tpl_list = templates.load_templates()
    uses = st.get("template_uses", {})
    if tpl_list:
        from .config import CONTRAST_TPL_IDS, TEMPORAL_TPL_IDS
        weights = [1.0 / (1.0 + uses.get(tid, 0)) for tid, _ in tpl_list]
        order = random.choices(tpl_list, weights=weights, k=min(4, len(tpl_list)))
        for tid, ttext in order:
            rel_tag = ("contrast" if tid in CONTRAST_TPL_IDS else
                       "temporal" if tid in TEMPORAL_TPL_IDS else None)
            candidates.append({"q": ttext.replace("{topic}", topic or "这些资料"),
                               "source": f"模板库[{tid}]", "rel_tag": rel_tag,
                               "kind": "template", "meta": tid})
    else:
        for ttext in random.sample(SEED_TEMPLATES, min(4, len(SEED_TEMPLATES))):
            candidates.append({"q": ttext.replace("{topic}", topic or "这些资料"),
                               "source": "种子模板", "rel_tag": None,
                               "kind": "template", "meta": None})

    # ④ v9.16 查重拦截：候选依次过查重，第一个非重复的才问
    templates.ensure_asked_texts(st)
    for cand in candidates:
        question = (cand["q"] or "").strip()
        if not question or templates.is_duplicate(question, st):
            continue
        if cand["kind"] in ("transform", "infer"):
            st["last_infer_round"] = st["rounds"]
            entry = {"qid": qid, "round": st["rounds"]}
            if cand["kind"] == "transform":
                expr_t, result_t = cand["meta"]
                entry.update({"premises": [expr_t], "result": result_t, "type": "transform"})
            else:
                A, C, D = cand["meta"]
                entry.update({"premises": [A, C], "result": D})
            il = st.setdefault("infer_log", [])
            il.append(entry)
            del il[:max(0, len(il) - 50)]
        elif cand["kind"] == "template" and cand["meta"]:
            tid = cand["meta"]
            st["template_uses"][tid] = st["template_uses"].get(tid, 0) + 1
            st.setdefault("tpl_pending", {})[qid] = {"tpl": tid, "round": st["rounds"]}
        return _write_question(st, qid, question, cand["source"], cand["rel_tag"], topic)
    # 全部候选都重复 → 拒绝提问。v9.16.1: 拦截同样计入提问节流（3 轮内不再重试，
    # 旧版拦截不推进节流，连拦 35 轮把 agent 锁死）；痛感 3 轮最多一次，不叠加
    st["dup_asks"] = st.get("dup_asks", 0) + 1
    st["asks_total"] = st.get("asks_total", 0) + 1
    st["last_ask_round"] = st["rounds"]
    mark(st, config.EVT_DUP_ASK)
    if st["rounds"] - st.get("last_dup_pain_round", -999) >= 3:
        feel(st, "satisfaction", config.PAIN_DUP_ASK)
        st["last_dup_pain_round"] = st["rounds"]
    return False, f"想问的都与近 {templates._ASKED_KEEP} 问重复（查重拦截，3 轮后再试）——本轮换个动作"


def do_read_brain(st):
    """读 outbox 未读回答：入笔记/入 kb/提取模板/VERDICT 回写 + ③ [RULE] 编译。"""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    read_any = False
    new_tpl = 0
    new_rules = 0
    for p in sorted(OUTBOX.glob("*.md")):
        if p.stem in st["brain_read"]:
            continue
        try:
            text = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        append_note(st, f"## 大脑回答 [{p.stem}]\n{text[:800]}")
        top_kw = "大脑回答"
        note, relate = semantic_note(text, f"brain/outbox/{p.stem}.md", top_kw, kind="answer")
        append_note(st, f"- 语义联想：{relate}")
        mark(st, config.EVT_READ_ANSWER)
        # 消化标注：回答与自身概念库的命中
        try:
            _ans_words = set(tokenizer.concept_words(text[:300])) 
            _hit_self = _ans_words & set(st.get("concepts", {}).keys())
            if len(_hit_self) >= 2:
                append_note(st, f"- 消化标注：内容命中自身概念库 {len(_hit_self)} 个（{'、'.join(list(_hit_self)[:4])}），与已有知识网络相关（自组织判定）")
            else:
                append_note(st, f"- 消化标注：自身概念库命中 {len(_hit_self)} 个（<2），视为新知识边界")
        except Exception:
            append_note(st, "- 消化标注：已消化")
        # ③ v9.16 [RULE] 编译（替代自动学噪音钩子：陈述进决策层，不再拉黑词汇）
        try:
            new_rules += rules_compiler.parse_and_compile(st, text, p.stem)
        except Exception:
            pass
        # 模板提取
        for tpl in templates.extract_templates(text):
            added, tpl_id = templates.save_template(tpl)
            if added:
                new_tpl += 1
                append_note(st, f"- 从大脑回答学到新模板 [{tpl_id}]：{tpl[:60]}")
        # 模板失败计数清零
        pending = st.get("tpl_pending", {})
        if p.stem in pending:
            tid = pending.pop(p.stem).get("tpl")
            if tid:
                st.setdefault("template_failures", {}).pop(tid, None)
        # VERDICT 回写（v9.7/v9.12：推理验证闭环）
        verdict = None
        for v in ("真", "假", "存疑"):
            if text.startswith(f"[VERDICT:{v}]"):
                verdict = v
                break
        if verdict:
            for il in st.get("infer_log", []):
                if il.get("qid") == p.stem and "verdict" not in il:
                    il["verdict"] = verdict
                    if il.get("type") == "transform":
                        expr_t = il["premises"][0] if il.get("premises") else ""
                        Dt = il.get("result")
                        if verdict == "真" and Dt:
                            tfs = st.setdefault("transforms", {})
                            tkey = expr_t + "→" + Dt
                            if tkey not in tfs:
                                tfs[tkey] = {
                                    "operands": re.findall(r"[一二三四五六七八九十]+", expr_t),
                                    "result": Dt, "support": 1, "last": st["rounds"],
                                    "source": "composed"}
                                append_note(st, f"- 变换习得：组合推导 {expr_t}={Dt} 判真，已注册为新变换")
                        append_note(st, f"- 推理验证：{expr_t}→{Dt} 被大脑判定为『{verdict}』")
                        break
                    prem = il.get("premises", [])
                    if len(prem) >= 2:
                        A, C = prem[0], prem[1]
                        D = il.get("result")
                        if verdict == "真" and D:
                            cc = st.setdefault("concepts", {}).setdefault(D, {"n": 0, "last": 0})
                            cc["n"] += 1
                            for k in list(st.get("relations", {}).keys()):
                                if D in k.split("|"):
                                    st["relations"][k]["w"] += 1
                        elif verdict == "假":
                            ak = "|".join(sorted([A, C]))
                            if ak in st.get("relations", {}):
                                st["relations"][ak]["w"] = max(0, st["relations"][ak]["w"] - 1)
                            _mk = st.setdefault("mistakes", [])
                            _mk.append({"round": st["rounds"], "premises": [A, C],
                                        "pattern": sorted([A, C]), "result": D})
                            del _mk[:-30]
                            mark(st, config.EVT_INFER_FALSE)
                        append_note(st, f"- 推理验证：{A}+{C}→{D if D else '?'} 被大脑判定为『{verdict}』")
                        break
        # 关系学习：Q-/FEED- 都学；相反/先后问题的回答给边打类型（⑤ 表征入口）
        # v9.16.1 路径5：自问自答对拍——提问前押的猜测，现在对着答案结算
        gs = st.get("q_self_guess", {}).pop(p.stem, None)
        if gs and gs.get("guess"):
            ans_words = set(tokenizer.concept_words(text))
            sq = st.setdefault("selfqa", {"hit": 0, "miss": 0})
            if gs["guess"] in ans_words:
                sq["hit"] += 1
                feel(st, "satisfaction", 0.05)
                append_note(st, f"- 自问自答：提问前我押《{gs['guess']}》会出现在答案里——猜中了（战绩 {sq['hit']}/{sq['hit'] + sq['miss']}）")
            else:
                sq["miss"] += 1
                feel(st, "satisfaction", -0.03)
                append_note(st, f"- 自问自答：我押《{gs['guess']}》会出现在答案里——没中（战绩 {sq['hit']}/{sq['hit'] + sq['miss']}）")
        # v9.16.1 问题4全量：相反/先后回答按句式收割 typed 边（提问→建图的断链接上）
        rel_tag = st.get("q_rel_tags", {}).pop(p.stem, None)
        rel_type, rel_topic = None, None
        if isinstance(rel_tag, dict):
            rel_type, rel_topic = rel_tag.get("type"), rel_tag.get("topic")
        elif isinstance(rel_tag, str):
            rel_type = rel_tag
        if rel_type and rel_topic:
            _harvest_typed_edges(st, text, rel_type, rel_topic)
        learn_from_text(st, text[:800], p.stem, p.stem, rel_type=rel_type)
        st["brain_read"].append(p.stem)
        read_any = True
    if not read_any:
        return False, "没有新回答"
    extra = f"，并新增 {new_tpl} 个提问模板" if new_tpl else ""
    extra += f"，编译 {new_rules} 条 [RULE]" if new_rules else ""
    return True, f"读到 {len([x for x in st['brain_read'] if x.startswith('Q-')])} 条大脑回答并记入笔记{extra}"


# ---------- 技能：搜索 / 网站 ----------

_CONTRAST_ANS_RE = re.compile(r"(?:反面|对立面|相反|对立)(?:的?(?:概念|东西|词|面))?(?:是|：|:)?\s*[『「《]?([^，。；、『」》]{2,10})")
_PRE_RE = re.compile(r"(?:之前|前提|先要|需要先|先有)(?:是|有)?\s*[『「《]?([^，。；、『」》]{2,10})")
_POST_RE = re.compile(r"(?:之后|接着|然后|会导致|引出|带来)(?:是|有)?\s*[『「《]?([^，。；、『」》]{2,10})")


def _harvest_typed_edges(st, text, rel_type, topic):
    """从相反/先后类回答里收割句式化关系边（v9.16.1 问题4全量）：
    "X 的反面是 Y" / "X 之前需要 Y" / "X 之后会导致 Z" → 直接建 typed 边，
    收割到的词由老师背书，直接晋升（这是策划入口，不是分词碰运气）。"""
    raw = []
    if rel_type == "contrast":
        raw += [m.group(1) for m in _CONTRAST_ANS_RE.finditer(text)]
    else:
        raw += [m.group(1) for m in _PRE_RE.finditer(text)]
        raw += [m.group(1) for m in _POST_RE.finditer(text)]
    made = 0
    rels = st.setdefault("relations", {})
    for w in dict.fromkeys(raw):
        clean = tokenizer.concept_words(w)
        if not clean:
            continue
        w2 = learning.promote_direct(st, clean.most_common(1)[0][0], max_len=10)
        if w2 and w2 != topic and w2 not in topic and topic not in w2:
            k = "|".join(sorted([topic, w2]))
            r = rels.setdefault(k, {"w": 0, "n": 0})
            r["w"] += 2
            r["n"] += 1
            r["last_touch"] = st["rounds"]
            r["type"] = rel_type
            made += 1
    if made:
        label = "相反" if rel_type == "contrast" else "先后"
        append_note(st, f"- 句式收割：从{label}回答里直接建出 {made} 条 {rel_type} 边（主题《{topic}》）")
    return made

def _pick_search_topic(st):
    """搜索主题（v9.16.1 知识缺口驱动，青梧路径4）：优先"被下注/被读到却没有边"
    的饥饿概念（度数最低），避开最近搜过的词——旧版拿 last_kw 首词，cache 连搜两次。"""
    rels = st.get("relations", {})
    cons = st.get("concepts", {})

    def degree(c):
        return sum(1 for k in rels if c in k.split("|"))

    recent = set(st.get("last_search_topics", [])[-5:])
    cands = []
    for p in st.get("edge_preds", []):
        if p.get("status") == "pending":
            cands += [p["a"], p["b"]]
    kw = st.get("last_kw", "")
    if kw and kw != "无":
        cands += [w for w in kw.split("、") if w in cons]
    hungry = [c for c in cands if c and c not in recent]
    if hungry:
        hungry.sort(key=lambda c: (degree(c), len(c)))
        return hungry[0][:24]
    # 回退链：last_kw 首词 → 文件名 → journey 尾词（旧行为）
    if kw and kw != "无" and recent:
        t = kw.split("、")[0][:24]
        if t and t not in recent:
            return t
    if st["visited_files"]:
        t = Path(st["visited_files"][-1]).name
        t = re.sub(r"\.[A-Za-z0-9]+$", "", t)
        if t and len(t) <= 24 and t not in recent:
            return t
    jf = NOTES / "journey.md"
    if jf.exists():
        for line in reversed(jf.read_text(encoding="utf-8", errors="ignore").splitlines()):
            line = line.strip()
            if line.startswith(("##", "- 语义联想")):
                continue
            words = tokenizer.concept_words(line)
            if words:
                t = words.most_common(1)[0][0][:24]
                if t not in recent:
                    return t
    return "涌现"


def do_web_search(st):
    """上网搜索（无 LLM 关键词级）：Bing 标题+摘要 → workspace/web/*.md + kb。"""
    ws = load_ask_config().get("web_search", {})
    if not ws.get("enabled", True):
        return False, "web_search 已禁用"
    topic = _pick_search_topic(st)
    st.setdefault("last_search_topics", []).append(topic)
    del st["last_search_topics"][:-5]      # 失败也记录，避免同词重试循环
    if any(bw in topic for bw in SEARCH_BLACK_WORDS):
        return False, "搜索词含黑名单词，已跳过"
    top_k = int(ws.get("top_k", 5))
    query = urllib.parse.quote(topic)
    url = f"https://cn.bing.com/search?q={query}&setlang=zh-hans"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        return False, f"搜索失败（{exc.__class__.__name__}），静默降级"
    results = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', html, re.S):
        block = m.group(0)
        hm = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not hm:
            continue
        link, title = hm.group(1), re.sub(r"<[^>]+>", "", hm.group(2)).strip()
        pm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = re.sub(r"<[^>]+>", "", pm.group(1)).strip() if pm else ""
        if title:
            results.append((title[:80], snippet[:200], link[:200]))
        if len(results) >= top_k:
            break
    if not results:
        return False, f"搜索「{topic}」无可用结果"
    WEB.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("web-%Y%m%d-%H%M")
    lines = [f"## 网络搜索 [{stamp}] 主题：{topic}", ""]
    for i, (title, snippet, link) in enumerate(results, 1):
        lines.append(f"{i}. {title}")
        if snippet:
            lines.append(f"   摘要：{snippet}")
        lines.append(f"   来源：{link}")
    text = "\n".join(lines)
    (WEB / f"{stamp}.md").write_text(text, encoding="utf-8")
    note, relate = semantic_note(text, f"workspace/web/{stamp}.md", topic, kind="web")
    append_note(st, f"## 网络搜索 [{stamp}] 主题「{topic}」\n{text[:600]}\n- 语义联想：{relate}")
    st["last_web_search_round"] = st["rounds"]
    mark(st, config.EVT_SEARCH)
    return True, f"网络搜索「{topic}」抓到 {len(results)} 条结果并入库"


def build_site(st):
    WEB.mkdir(parents=True, exist_ok=True)
    emo = {k: round(v, 2) for k, v in st["emotions"].items()}
    notes_text = ""
    jf = NOTES / "journey.md"
    if jf.exists():
        notes_text = jf.read_text(encoding="utf-8")[:4000]
    files_list = ""
    if ARCHIVE.exists():
        files_list = "".join(f"<li>{p.relative_to(ARCHIVE)}</li>" for p in ARCHIVE.rglob("*") if p.is_file())
    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>selfdrive agent 探索站</title>
<style>
body{{font-family:monospace;max-width:720px;margin:40px auto;padding:0 16px;color:#222;background:#fff}}
h1{{border-bottom:2px solid #000;padding-bottom:8px}}
.box{{border:1px solid #000;padding:12px;margin:14px 0}}
li{{margin:4px 0}}
</style></head><body>
<h1>selfdrive agent 探索站</h1>
<p>第 {st['rounds']} 轮 · 情感：好奇 {emo.get('curiosity', 0)} · 无聊 {emo.get('boredom', 0)} · 满足 {emo.get('satisfaction', 0)} · 疲劳 {emo.get('fatigue', 0)}</p>
<div class="box"><b>探索统计</b><ul>
<li>已读文件：{len(st['visited_files'])}</li>
<li>外部文件发现：{len(st['wider_files'])}</li>
<li>笔记行数：{st['note_lines']}</li>
<li>网页刷新：{st['web_refreshes']}</li>
<li>已提问题：{len(st['brain_questions'])}（查重拦截 {st.get('dup_asks', 0)} 次）</li>
<li>已读回答：{len([x for x in st['brain_read'] if x.startswith('Q-')])}</li>
<li>概念（晋升制）：{len(st.get('concepts', {}))} + 候选 {len(st.get('word_cands', {}))}</li>
<li>可失败预测：{predictions.pred_stats(st)}</li>
<li>编译规则：{len(st.get('compiled_rules', {}))} 条</li>
<li>web 缓存配额：{web_share(st):.0%} / {config.WEB_SHARE_CAP:.0%}</li>
</ul></div>
<div class="box"><b>口粮资料</b><ul>{files_list or '<li>无</li>'}</ul></div>
<div class="box"><b>探索日记</b><pre>{notes_text}</pre></div>
</body></html>"""
    (WEB / "index.html").write_text(html, encoding="utf-8")
    st["web_built"] = True
    if not server_running():
        try:
            log_f = open(LOGS / "http.log", "a")
            subprocess.Popen(["python3", "-m", "http.server", "8000", "--directory", str(WEB)],
                             stdout=log_f, stderr=log_f, start_new_session=True)
        except Exception as exc:
            return True, f"网站已生成但服务启动失败：{exc}"
    return True, "网站已部署（index.html + http.server:8000）"


def refresh_site(st):
    st["web_refreshes"] += 1
    return build_site(st)


def server_running():
    try:
        out = subprocess.run(["pgrep", "-f", "http.server 8000"], capture_output=True, text=True, timeout=5)
        return bool(out.stdout.strip())
    except Exception:
        return False


def do_rest(st):
    return True, "休息一轮（疲劳下降）"


# ---------- 技能：记忆整理 ----------

def _replay_line(st, lines):
    """间隔重放（青梧路径2）：优先挑最近没重放过的行，别对同一行反复回味。"""
    recent = st.setdefault("recent_replay", [])
    ln = random.choice(lines)
    for _ in range(6):
        if ln not in recent:
            break
        ln = random.choice(lines)
    recent.append(ln)
    del recent[:-10]
    return ln


def do_consolidate_memory(st):
    """记忆巩固：重放 + 修剪弱边/规则 + ⑤ 概念候选修剪 + ② 预测超时结算。"""
    replayed = 0
    jf = NOTES / "journey.md"
    if jf.exists():
        try:
            lines = [l.strip() for l in jf.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
            if lines:
                for _ in range(2):
                    ln = _replay_line(st, lines)
                    append_note(st, f"- 记忆重放：{ln[:120]}")
                    replayed += 1
        except Exception:
            pass
    st.setdefault("consolidate_round", st["rounds"])
    before_r = len(st.get("relations", {}))
    rm = [k for k, v in st["relations"].items()
          if v.get("w", 0) <= 1 and (st["rounds"] - v.get("last_touch", st["rounds"])) > 300]
    for k in rm[:20]:
        del st["relations"][k]
    rm_rules = 0
    for k, v in list(st.get("rules", {}).items()):
        if v["status"] == "pending" and v["support"] >= 3 and (st["rounds"] - v["first"]) > 50:
            v["status"] = "confirmed_stat"
            v["confidence"] = min(0.6, v["confidence"] + 0.1)
        elif v["confidence"] < 0.3 and (st["rounds"] - v["first"]) > 30:
            del st["rules"][k]
            rm_rules += 1
        elif v["status"] == "pending" and v["support"] <= 2 and (st["rounds"] - v["first"]) > 200:
            del st["rules"][k]
            rm_rules += 1
    st["consolidate_round"] = st["rounds"]
    # 概念层级（v9.9）
    hier = st.setdefault("hierarchy", {})
    _cs = list(st.get("concepts", {}).keys())
    _added = 0
    for a in _cs:
        if len(a) < 2:
            continue
        for b in _cs:
            if a != b and len(b) >= len(a) + 2 and a in b:
                h = hier.setdefault(a, [])
                if b not in h:
                    h.append(b)
                    _added += 1
                if len(h) > 8:
                    del h[:-8]
        if _added >= 40:
            break
    pruned = learning.prune_concepts(st)
    swept = learning.sweep_legacy_fragments(st)
    expired = predictions.expire_edge_predictions(st)
    return True, (f"记忆巩固：重放 {replayed} 条、修剪关系 {len(rm)} 条、规则库 {len(st.get('rules', {}))} 条"
                  f"（回收噪音规则 {rm_rules} 条，层级 {_added} 条，概念修剪 {pruned} 个，遗产碎片合并 {swept} 对，预测落空 {expired} 个）")


def do_reflect_memory(st):
    """辉月回望：重放记忆 + 多类型关系标注（causal/contrast/similar/temporal）。"""
    from .config import CONTRAST_PAIRS
    replayed = []
    jf = NOTES / "journey.md"
    if jf.exists():
        try:
            lines = [l.strip() for l in jf.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
            if lines:
                for _ in range(2):
                    ln = _replay_line(st, lines)
                    replayed.append(ln[:80])
                    append_note(st, f"- 辉月回想：{ln[:120]}")
        except Exception:
            pass
    rels = st.get("relations", {})
    top_edge = None
    if rels:
        top_edge = max(rels.items(), key=lambda kv: kv[1].get("w", 0) if isinstance(kv[1], dict) else 0)
    conf_rules = [v for v in st.get("rules", {}).values() if v.get("status") == "confirmed_stat"]
    if replayed or top_edge or conf_rules:
        _rl = st.setdefault("reflect_log", [])
        _rl.append({
            "round": st["rounds"],
            "what": f"回想{len(replayed)}条；最强边：{top_edge[0] if top_edge else '无'}；确认规则{len(conf_rules)}条",
            "built": {},
        })
        del _rl[:max(0, len(_rl) - 100)]
    built = {"causal": 0, "contrast": 0, "similar": 0, "temporal": 0}
    # causal：confirmed 规则 前提→结论
    concepts_set = set(st.get("concepts", {}))
    for v in conf_rules:
        prem, concl = v.get("premises", []), v.get("conclusion", "")
        for p in prem:
            if p and concl and p != concl:
                k = "|".join(sorted([p, concl]))
                r = rels.get(k)
                if r is None:
                    if p in concepts_set or concl in concepts_set:
                        rels[k] = {"w": 1, "n": 1, "last_touch": st["rounds"], "type": "causal"}
                        built["causal"] += 1
                        break
                elif r.get("type") in (None, "co_occur"):
                    r["type"] = "causal"
                    built["causal"] += 1
                    break
    # contrast：强邻指向相反词对
    if rels:
        keys = list(rels.keys())
        sample = random.sample(keys, min(6, len(keys)))
        pairset = {(a, b) for a, b in CONTRAST_PAIRS} | {(b, a) for a, b in CONTRAST_PAIRS}

        def _nbrs(c):
            out = {}
            for k in rels:
                a, b = k.split("|")
                if a == c:
                    out[b] = out.get(b, 0) + rels[k].get("w", 0)
                elif b == c:
                    out[a] = out.get(a, 0) + rels[k].get("w", 0)
            return out

        for k in sample:
            a, b = k.split("|")
            if a == b:
                continue
            na, nb = _nbrs(a), _nbrs(b)
            topa = sorted(na.items(), key=lambda x: -x[1])[:3]
            topb = sorted(nb.items(), key=lambda x: -x[1])[:3]
            hit = False
            for ca, _ in topa:
                for cb, _ in topb:
                    if (ca, cb) in pairset:
                        if rels[k].get("type") in (None, "co_occur"):
                            rels[k]["type"] = "contrast"
                            built["contrast"] += 1
                        hit = True
                        break
                if hit:
                    break
    # similar：共享邻居 Jaccard ≥ 0.4
    if rels:
        keys = list(rels.keys())
        sample = random.sample(keys, min(8, len(keys)))
        for k in sample:
            a, b = k.split("|")
            if a == b:
                continue
            na, nb = _nbrs(a), _nbrs(b)
            inter = set(na) & set(nb)
            union = set(na) | set(nb)
            if union and len(inter) / len(union) >= 0.4 and len(inter) >= 1:
                if rels[k].get("type") in (None, "co_occur"):
                    rels[k]["type"] = "similar"
                    built["similar"] += 1
    # temporal：旅程相邻段落首概念对（先→后）
    if jf.exists():
        try:
            segs, cur = [], []
            for l in jf.read_text(encoding="utf-8", errors="ignore").splitlines():
                if l.strip().startswith("##"):
                    if cur:
                        segs.append(" ".join(cur))
                        cur = []
                cur.append(l.strip())
            if cur:
                segs.append(" ".join(cur))
            cseq = []
            for s in segs:
                wc = tokenizer.concept_words(s)
                if wc:
                    cseq.append(wc.most_common(1)[0][0])
            for i in range(min(3, len(cseq) - 1)):
                A, B = cseq[i], cseq[i + 1]
                if A and B and A != B:
                    k = "|".join(sorted([A, B]))
                    if k in rels and rels[k].get("type") in (None, "co_occur"):
                        rels[k]["type"] = "temporal"
                        built["temporal"] += 1
        except Exception:
            pass
    if st.get("reflect_log"):
        st["reflect_log"][-1]["built"] = built
    dist = Counter(r.get("type", "co_occur") for r in rels.values())
    dist_s = " ".join(f"{t}:{dist.get(t, 0)}" for t in ("co_occur", "causal", "contrast", "similar", "temporal"))
    return True, (f"辉月回望：回想 {len(replayed)} 条记忆、关系标注 {sum(built.values())} 条"
                  f"（因果 {built['causal']} / 相反 {built['contrast']} / 相似 {built['similar']} / 时序 {built['temporal']}）｜关系分布：{dist_s}")


def _virtual_experiment(st):
    """虚拟实验（青梧路径1 仿生）：挑一条因果边（没有则挑强边）A—B，
    "屏蔽 A"看 B 还剩几条独立连接——像婴儿拨开吊饰看还动不动：
    B 几乎只剩 A → 因果嫌疑；B 有别的支撑 → 可能只是相关。
    这是它第一次靠"操作"而不只是"共现统计"来区分因果与相关。"""
    rels = st.get("relations", {})
    if len(rels) < 6:
        return None
    causal = [k for k, v in rels.items() if v.get("type") == "causal"]
    pool = causal if causal else sorted(rels, key=lambda k: -rels[k].get("w", 0))[:10]
    k = random.choice(pool)
    a, b = k.split("|")

    def others_of(x, exclude):
        out = []
        for kk in rels:
            p, q = kk.split("|")
            if p == x and q != exclude:
                out.append(q)
            elif q == x and p != exclude:
                out.append(p)
        return out

    b_others = others_of(b, a)
    suspicion = len(b_others) <= 1
    if suspicion:
        conclusion = (f"虚拟实验：把《{a}》从记忆里暂时屏蔽，《{b}》只剩 {len(b_others)} 条别的连接——"
                      f"它几乎全靠《{a}》支撑，因果关系嫌疑大。值得拿着去问老师验证。")
    else:
        conclusion = (f"虚拟实验：把《{a}》屏蔽后，《{b}》仍有 {len(b_others)} 条独立连接"
                      f"（{('、'.join(b_others[:3]))}…）——两者可能只是相关，不是因果。")
    return {"pair": [a, b], "conclusion": conclusion, "causal_suspicion": suspicion}


def do_self_talk(st):
    """自言自语 v9.16.1 双模式：
    ① 下注（可失败预测）：对无边概念对押"有关联"，同现兑现/超时落空；
    ② 虚拟实验（50%）：屏蔽 A 看 B 的支撑，区分因果与相关。
    旧版抽强边猜共同邻居 30/30 全命中（零信息）——已废弃。"""
    st.setdefault("self_talk_log", [])
    if random.random() < 0.5:
        exp = _virtual_experiment(st)
        if exp:
            st["self_talk_log"].append({
                "round": st["rounds"], "pair": exp["pair"], "type": "experiment",
                "causal_suspicion": exp["causal_suspicion"]})
            del st["self_talk_log"][:-30]
            append_note(st, f"- {exp['conclusion']}")
            return True, (f"自言自语（虚拟实验）：{exp['pair'][0]}—{exp['pair'][1]}，"
                          f"因果嫌疑={'是' if exp['causal_suspicion'] else '否'}")
    pair = predictions.make_edge_prediction(st)
    if not pair:
        return False, "没有可下注的无边概念对（概念不足或已在赌的太多）"
    a, b = pair
    st["self_talk_log"].append({"round": st["rounds"], "pair": [a, b], "bet": "link",
                                "horizon": config.PRED_HORIZON})
    del st["self_talk_log"][:-30]
    append_note(st, f"- 自言自语：想到《{a}》和《{b}》——它们在我的概念网里还没有边。"
                    f"我赌它们其实有关联（{config.PRED_HORIZON} 轮内会一起出现）。下注完成，等世界判对错。")
    return True, f"自言自语：下注《{a}》↔《{b}》有关联（无边对，可失败预测）"
