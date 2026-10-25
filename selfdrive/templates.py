# -*- coding: utf-8 -*-
"""templates — 提问模板库 + 提问查重（v9.16 拆出并加强）。

v9.16 关键修复：
  - 查重 is_duplicate：新问题与最近 30 问的字符二元组 Jaccard ≥0.7 判重。
    直接攻击"51% 重复、25% 是三个种子句"的老病（ask_brain 里查重拦截 + 痛感）。
  - prune_templates 不再淘汰内置模板（相反/先后）——旧版把学来的模板淘汰光后
    回退种子句，是复读的直接原因之一。
"""
import re
import time

from . import config, teacherless
from .config import (BUILTIN_TEMPLATE_IDS, INBOX, PRUNE_EVERY, PRUNE_KEEP_MIN,
                     SEED_TEMPLATES, TEMPLATES_DIR, TEMPLATE_MAX)

_ASKED_KEEP = 30        # 查重窗口
_DUP_THRESHOLD = 0.7    # 字符二元组 Jaccard 阈值


def load_templates():
    """读模板库：[(模板id, 模板文本)]，按文件名排序保证稳定。"""
    if not TEMPLATES_DIR.exists():
        return []
    items = []
    for p in sorted(TEMPLATES_DIR.glob("*.txt")):
        try:
            text = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if text:
            items.append((p.stem, text))
    return items


def save_template(text):
    """把新模板写入模板库（去重后）。返回 (是否新增, 模板id)。"""
    text = (text or "").strip()
    if not text or len(text) > 200:
        return False, None
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_templates()
    norm = text.replace(" ", "").replace("\n", "")
    for _, t in existing:
        if t.replace(" ", "").replace("\n", "") == norm:
            return False, None
    idx = len(existing) + 1
    tpl_id = f"t{idx:04d}"
    used = {tid for tid, _ in existing}
    while tpl_id in used:
        idx += 1
        tpl_id = f"t{idx:04d}"
    (TEMPLATES_DIR / f"{tpl_id}.txt").write_text(text, encoding="utf-8")
    return True, tpl_id


def extract_templates(text):
    """从大脑回答中提取 [TEMPLATE]...[/TEMPLATE] 标记块。"""
    return re.findall(r"\[TEMPLATE\]\s*(.*?)\s*\[/TEMPLATE\]", text, re.S)


def prune_templates(st):
    """模板库演化：容量超限时淘汰低频旧模板（内置模板豁免）。"""
    if st["rounds"] - st["template_prune_round"] < PRUNE_EVERY:
        return 0
    st["template_prune_round"] = st["rounds"]
    items = [(tid, t) for tid, t in load_templates() if tid not in BUILTIN_TEMPLATE_IDS]
    if len(items) <= TEMPLATE_MAX:
        return 0

    def key(item):
        tid, _ = item
        return (st["template_uses"].get(tid, 0), tid)
    items.sort(key=key)
    removed = 0
    for tid, _ in items[:-PRUNE_KEEP_MIN]:
        p = TEMPLATES_DIR / f"{tid}.txt"
        if p.exists():
            p.unlink()
            st["template_uses"].pop(tid, None)
            removed += 1
        if len(items) - removed <= TEMPLATE_MAX:
            break
    return removed


def check_template_retirement(st):
    """Ratchet 式退役：模板通道提问后 6 轮未读到回答 → 无果+1；累计 4 次 → 退役。"""
    pending = st.get("tpl_pending", {})
    if not pending:
        return
    now = st["rounds"]
    expired = [q for q, info in pending.items() if now - info.get("round", 0) > 6]
    for q in expired:
        info = pending.pop(q)
        tid = info.get("tpl")
        if not tid:
            continue
        tf = st.setdefault("template_failures", {})
        tf[tid] = tf.get(tid, 0) + 1
        if tf[tid] >= 4 and tid not in BUILTIN_TEMPLATE_IDS:
            tpl_file = TEMPLATES_DIR / f"{tid}.txt"
            if tpl_file.exists():
                retired_dir = TEMPLATES_DIR / "retired"
                retired_dir.mkdir(exist_ok=True)
                try:
                    tpl_file.rename(retired_dir / f"{tid}.txt")
                    from .state import append_note
                    append_note(st, f"- 模板 [{tid}] 退役：连续 {tf[tid]} 次提问无果，移入 retired/（可恢复）")
                    tf.pop(tid, None)
                except Exception:
                    pass


BUILTIN_TEMPLATE_TEXTS = {
    "seed-contrast": "《{topic}》的反面或对立面是什么？什么情况下它会不成立？",
    "seed-temporal": "要出现《{topic}》，之前通常需要什么条件？它之后又会引出什么？（说说先后）",
}


def ensure_builtin_templates():
    """确保内置"相反/先后"模板存在（contrast/temporal 表征的提问入口）。"""
    for tid, text in BUILTIN_TEMPLATE_TEXTS.items():
        p = TEMPLATES_DIR / f"{tid}.txt"
        if not p.exists():
            try:
                TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
            except Exception:
                pass


def ensure_asked_texts(st):
    """查重窗口为空时从 inbox.md 回填最近提问（老状态升级/首次运行）。
    Teacherless：inbox 是教师通道，不许把历史提问洗成自己的查重窗口。"""
    if teacherless.is_enabled():
        st.setdefault("asked_texts", [])
        return
    if st.get("asked_texts"):
        return
    texts = []
    if INBOX.exists():
        try:
            for line in INBOX.read_text(encoding="utf-8", errors="ignore").splitlines()[-200:]:
                m = re.match(r"^\[(Q-[^\]]+)\]\s*(.+)$", line.strip())
                if m:
                    texts.append(m.group(2).strip())
        except Exception:
            pass
    st["asked_texts"] = texts[-_ASKED_KEEP:]


def _bigrams(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s} if s else set()


def is_duplicate(question, st):
    """判重（v9.23.3 三层）：①一字不差；②骨架相似且《》主题/残差重合；
    ③主题级全量查重——问题核心概念集合与【全部历史问题】重合 ≥0.6 判重
    （"探索的反面"和"探索的先后"字面不同但主题相同，字面查重拦不住）。"""
    from .tokenizer import concept_words
    ensure_asked_texts(st)
    # 主题词 = 概念集 − 模板句式词（反面/对立面/条件是句式骨架，不算主题）
    TEMPLATE_WORDS = {"反面", "对立面", "成立", "条件", "引出", "先后", "通常",
                      "需要", "说说"}
    qc = set(concept_words(question)) - TEMPLATE_WORDS
    if qc:
        for entry in st.get("asked_concepts", []):
            old_cs = set(entry.get("cs", [])) - TEMPLATE_WORDS
            if not old_cs or not qc:
                continue
            # 主题词交集非空且包含度 ≥0.5 → 同一主题变体
            inter = qc & old_cs
            if inter and len(inter) / min(len(qc), len(old_cs)) >= 0.5:
                return True
    # 原有两层（骨架 + 《》主题/残差）
    """判重（v9.16.1 修正）：骨架相似【且】内容相同才算重复。
    旧版只比全文二元组——"《信号》的反面"和"《cache》的反面"骨架 Jaccard 0.70，
    新模板刚学会就被锁死（青梧诊断问题1：71% 误拦）。
    内容怎么取：①优先比《》引用的主题词；②无引用时，剥掉所有模板骨架后比残差词。
    骨架相同、主题不同 → 放行（它是在练新模板，不是复读）。"""
    ensure_asked_texts(st)
    q_spans = _topic_spans(question)
    for old in st.get("asked_texts", [])[-_ASKED_KEEP:]:
        if question.strip() == old.strip():
            return True                                 # 一字不差 → 必重
        og = _bigrams(old)
        qg = _bigrams(question)
        if not og or not qg:
            continue
        if len(qg & og) / len(qg | og) < _DUP_THRESHOLD:
            continue                                    # 句式骨架都不同 → 放行
        o_spans = _topic_spans(old)
        if q_spans or o_spans:
            # 有引用主题：主题集合相似才算重复
            if q_spans and o_spans and _jaccard(q_spans, o_spans) >= 0.5:
                return True
            continue                                    # 一方有主题一方没有 → 放行
        qr, orr = _skeleton_residual(question), _skeleton_residual(old)
        if qr and orr and _jaccard(qr, orr) >= 0.6:
            return True                                 # 剥骨架后剩下的还是同一些词
        if not qr and not orr:
            return True                                 # 双方剥完骨架全空 → 纯骨架复读
    return False


def _topic_spans(q):
    """问题里《》引用的主题词集合。"""
    return {s.strip() for s in re.findall(r"《([^》]+)》", q) if s.strip()}


def _skeleton_residual(q):
    """剥掉所有已知模板骨架（含种子）后剩下的内容词集合。"""
    from .tokenizer import concept_words
    residual = q
    skeletons = [t for _, t in load_templates()] + list(SEED_TEMPLATES)
    for t in skeletons:
        for part in t.split("{topic}"):
            part = part.strip()
            if len(part) >= 2:
                residual = residual.replace(part, " ")
    return set(concept_words(residual))


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def remember_ask(st, question):
    st.setdefault("asked_texts", []).append(question.strip())
    del st["asked_texts"][:-_ASKED_KEEP]
    # v9.23.3 主题账本：全量记录每个问题的概念词集合（防"滑出窗口后重问"）
    from .tokenizer import concept_words
    cs = sorted(set(concept_words(question)))[:8]
    st.setdefault("asked_concepts", []).append({"q": question.strip()[:60], "cs": cs})
    del st["asked_concepts"][:-200]
    st["asks_total"] = st.get("asks_total", 0) + 1


def dup_rate(st):
    """重复率指标（"听懂测试"与 [RULE] 出生快照用）。"""
    total = st.get("asks_total", 0)
    if not total:
        return 0.0
    return st.get("dup_asks", 0) / total
