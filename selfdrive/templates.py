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

from . import config
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
    """查重窗口为空时从 inbox.md 回填最近提问（老状态升级/首次运行）。"""
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
    """与最近 _ASKED_KEEP 问的字符二元组 Jaccard ≥ 阈值 → 判重。"""
    ensure_asked_texts(st)
    qg = _bigrams(question)
    if not qg:
        return False
    for old in st.get("asked_texts", [])[-_ASKED_KEEP:]:
        og = _bigrams(old)
        if not og:
            continue
        j = len(qg & og) / len(qg | og)
        if j >= _DUP_THRESHOLD:
            return True
    return False


def remember_ask(st, question):
    st.setdefault("asked_texts", []).append(question.strip())
    del st["asked_texts"][:-_ASKED_KEEP]
    st["asks_total"] = st.get("asks_total", 0) + 1


def dup_rate(st):
    """重复率指标（"听懂测试"与 [RULE] 出生快照用）。"""
    total = st.get("asks_total", 0)
    if not total:
        return 0.0
    return st.get("dup_asks", 0) / total
