#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfdrive agent v6 — 语义读取（自编码器 + 语义记忆库）
  - 新增 semantics.py：纯 Python 单隐层自编码器（512→32→512），在线自监督训练，零依赖
  - 探索/读回答升级：读全文 → 向量化 → 32 维"语义指纹" → 与记忆库比对 → 联想笔记
  - 好奇驱动选择：有多个候选文件时，优先读"与已读内容最不同"的（读没见过的东西）
  - 其余沿用 v5：学习调制、提问饥饿、模板库自增长、大脑桥接
文件约定：
  ~/selfdrive/brain/inbox.md            agent 追加问题：[Q-编号] 问题内容
  ~/selfdrive/brain/outbox/Q-编号.md      大脑回答（文件名 = 问题编号）
  ~/selfdrive/brain/templates/t0001.txt   提问模板库（一个文件一个模板，含 {topic} 占位）
  ~/selfdrive/semantics/ae.json           自编码器参数（在线学习，读得越多越会抓结构）
  ~/selfdrive/semantics/memory.jsonl      语义记忆库（每篇文本的 32 维指纹 + 关键词）
"""
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
WORKSPACE = BASE / "workspace"
ARCHIVE = WORKSPACE / "archive"
NOTES = WORKSPACE / "notes"
WEB = WORKSPACE / "web"
BRAIN = BASE / "brain"
INBOX = BRAIN / "inbox.md"
OUTBOX = BRAIN / "outbox"
TEMPLATES_DIR = BRAIN / "templates"          # 可插拔提问模板库（每个 .txt = 一个模板）
TEMPLATE_STATS_FILE = BRAIN / "template_stats.json"
LOGS = BASE / "logs"
STATE_FILE = BASE / "state.json"
INTERVAL = int(os.environ.get("SELFDRIVE_INTERVAL", "300"))
ONCE = "--once" in sys.argv

import semantics as sem   # v6：语义读取引擎（纯 Python 自编码器 + 语义记忆库）
SEM = sem.Semantics(BASE)

# 模板库演化参数
TEMPLATE_MAX = 15          # 模板库容量上限
PRUNE_EVERY = 25           # 每 N 轮整理一次模板库
PRUNE_KEEP_MIN = 5         # 淘汰后至少保留的模板数
SEED_TEMPLATES = [         # 内置种子模板（模板库为空时的后备）
    "我读了《{topic}》，它的核心意思是什么？有什么我没注意到的要点？",
    "我探索了这些资料，它们之间有什么联系？",
    "除了我现在读的这些东西，还有哪些值得探索的方向？",
    "我现在应该尝试做什么新的事情？",
]

WIDER_ROOTS = ["/home/qingwu", "/usr/share/doc"]
WIDER_EXTS = {".txt", ".md", ".py", ".json", ".html", ".rst", ".log"}
BLACKLIST_DIRS = {".ssh", ".cache", ".git", "venv", ".venv", "__pycache__", "node_modules",
                  ".config", ".local", ".npm", ".java", ".julia", ".rustup", ".cargo"}

EMOTIONS = ["curiosity", "boredom", "satisfaction", "fatigue"]
ACTIONS = ["explore_file", "explore_wider", "visit_new_dir", "tidy_notes",
           "build_site", "refresh_site", "ask_brain", "read_brain", "rest"]


def default_state():
    return {
        "emotions": {"curiosity": 0.3, "boredom": 0.1, "satisfaction": 0.2, "fatigue": 0.0},
        "visited_files": [], "visited_dirs": [], "wider_files": {},
        "action_counts": {}, "note_lines": 0, "rounds": 0,
        "web_built": False, "web_refreshes": 0,
        "brain_questions": [], "brain_read": [], "last_ask_round": 0,
        "template_uses": {}, "template_prune_round": 0,
        "action_stats": {},   # v5：学习调制（{动作: {ok, try}}）
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def load_state():
    d = default_state()
    if STATE_FILE.exists():
        try:
            old = json.loads(STATE_FILE.read_text())
            for k in d:
                if k in old:
                    d[k] = old[k]
        except Exception:
            pass
    return d


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def log_event(st, action, detail):
    LOGS.mkdir(exist_ok=True)
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "round": st["rounds"],
           "emotions": {k: round(v, 3) for k, v in st["emotions"].items()},
           "action": action, "detail": detail}
    with open(LOGS / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def scan_workspace():
    files, dirs = [], []
    if ARCHIVE.exists():
        for p in ARCHIVE.rglob("*"):
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


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def update_emotions(st, action, novelty, success):
    e = st["emotions"]
    e["curiosity"] = clamp(e["curiosity"] + 0.12 * novelty - 0.04)
    e["boredom"] = clamp(e["boredom"] + (0.06 if action in ("explore_file", "tidy_notes", "refresh_site") else -0.08) - 0.02)
    e["satisfaction"] = clamp(e["satisfaction"] + 0.15 * success - 0.03)
    if action == "build_site" and success:
        e["satisfaction"] = clamp(e["satisfaction"] + 0.4)
        e["curiosity"] = clamp(e["curiosity"] + 0.15)
    if action == "read_brain" and success:
        e["satisfaction"] = clamp(e["satisfaction"] + 0.3)
        e["curiosity"] = clamp(e["curiosity"] + 0.1)
    if action == "ask_brain" and success:
        e["curiosity"] = clamp(e["curiosity"] - 0.05)  # 求知有了出口
    e["fatigue"] = clamp(e["fatigue"] + (0.05 if action != "rest" else -0.35))
    if e["curiosity"] < 0.2:
        e["boredom"] = clamp(e["boredom"] + 0.05)
    return e


def pick_action(st, e, web_built):
    """决策模型 v5：基线权重 + 学习调制 + 提问饥饿调制。"""
    w = {
        "explore_file": e["curiosity"] * (1 - e["boredom"]) + 0.2,
        "explore_wider": e["curiosity"] * 0.9 + 0.1,
        "visit_new_dir": e["curiosity"] * 0.7 + 0.1,
        "tidy_notes": e["satisfaction"] * 0.4 + e["boredom"] * 0.5 + 0.1,
        "build_site": (0.55 - e["boredom"] * 0.3) if not web_built else 0.05,
        "refresh_site": e["satisfaction"] * 0.6 + 0.08 if web_built else 0.05,
        "ask_brain": e["curiosity"] * 0.6 + e["boredom"] * 0.2,
        "read_brain": e["satisfaction"] * 0.4 + 0.15,
        "rest": e["fatigue"] * 2.0 + (1 - e["curiosity"]) * 0.2,
    }
    # ① 学习调制：动作历史成功率动态调权重（尝试≥3 次才生效）
    stats = st.get("action_stats", {})
    for a in w:
        s = stats.get(a, {"ok": 0, "try": 0})
        if s["try"] >= 3:
            rate = s["ok"] / s["try"]
            if rate >= 0.5:
                w[a] *= 1.3      # 顺手 → 更想再做
            elif rate <= 0.25 and s["try"] >= 6:
                w[a] *= 0.6      # 屡屡碰壁 → 兴趣下降
    # ② 提问饥饿：距上次提问越久越想问；从未提问过 → 保底渴望
    hunger = min(1.0, (st["rounds"] - st["last_ask_round"]) / 6.0)
    w["ask_brain"] *= 1.0 + hunger * 1.5
    if not st["brain_questions"]:
        w["ask_brain"] += 0.5
    # ③ 有未读回答时优先读（每封未读 ×0.5，封顶 ×2）
    unread = []
    if OUTBOX.exists():
        unread = [p for p in OUTBOX.glob("*.md") if p.stem not in st["brain_read"]]
    if unread:
        w["read_brain"] *= 1.0 + min(1.0, len(unread) * 0.5)
    names = list(w.keys())
    return random.choices(names, weights=[w[n] for n in names], k=1)[0]


def read_head(path, n=20):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return [l.rstrip() for l in f.readlines()[:n]]
    except Exception:
        return []


def read_full(path, max_chars=sem.MAX_CHARS):
    """v6：读全文（截断到上限字符）供语义化。"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read(max_chars)
    except Exception:
        return ""


def semantic_note(full_text, path, top, kind="text"):
    """v6：全文语义化 → 记忆库联想。返回 (联想笔记文本, detail 后缀)。"""
    vec = sem.text_to_vec(full_text)
    fp = SEM.learn(vec, str(path), top, kind=kind)
    sim, item = SEM.closest(fp)
    if item and sim > 0.30:
        other = Path(item["path"]).name
        relate = f"与《{other}》最像（相似度 {sim:.2f}）"
        note = f"\n- 语义联想：{relate}，关键词：{item['keywords'][:40]}"
    else:
        relate = f"与已读最像的仅 {sim:.2f}，全新主题"
        note = f"\n- 语义联想：{relate}，值得记录"
    return note, relate


def append_note(st, text):
    NOTES.mkdir(parents=True, exist_ok=True)
    with open(NOTES / "journey.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")
    st["note_lines"] += 1


def do_explore_file(files, st):
    unseen = [f for f in files if f["path"] not in st["visited_files"]]
    pool = unseen if unseen else files
    if not pool:
        return False, "无文件可读"
    if len(pool) > 1 and SEM.memory:
        # v6 好奇驱动选择：读"与已读内容最不同"的文件（读没见过的东西）
        def novelty_of(f):
            vec = sem.text_to_vec(read_full(BASE / f["path"]))
            fp = SEM.ae.encode(vec)
            return 1.0 - SEM.avg_sim(fp)
        try:
            pool = [max(pool, key=novelty_of)]
        except Exception:
            pass
    f = pool[0]
    full = read_full(BASE / f["path"])
    lines = full.splitlines()
    title = lines[0][:60] if lines else f["name"]
    words = Counter(re.findall(r"[\u4e00-\u9fa5]{2,}", " ".join(lines[:40])))
    top = "、".join(w for w, _ in words.most_common(8)) or "无"
    note, relate = semantic_note(full, BASE / f["path"], top)
    append_note(st, f"## 读到：{f['name']}\n- 首行：{title}\n- 关键词：{top}{note}")
    if f["path"] not in st["visited_files"]:
        st["visited_files"].append(f["path"])
    return True, f"读了 {f['name']}（{len(lines)} 行，关键词：{top}，{relate}）"


def do_explore_wider(st):
    found = scan_wider(st)
    if not found:
        return False, "更大范围没有新文本文件"
    key = random.choice(found)
    try:
        full = read_full(key)
        lines = full.splitlines()
        words = Counter(re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{4,}", " ".join(lines[:40])))
        top = "、".join(w for w, _ in words.most_common(8)) or "无"
        note, relate = semantic_note(full, key, top)
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
        words = Counter(re.findall(r"[\u4e00-\u9fa5]{2,}", text))
        top = "、".join(w for w, _ in words.most_common(10))
    else:
        top = "暂无笔记"
    with open(NOTES / "index.md", "w", encoding="utf-8") as f:
        f.write(f"# 探索索引（第 {st['rounds']} 轮）\n高频词：{top}\n已读 {len(st['visited_files'])} 个文件\n")
    # 顺带整理模板库（低频淘汰）
    removed = prune_templates(st)
    extra = f"，模板库淘汰 {removed} 个低频模板" if removed else ""
    return True, f"整理笔记（高频词：{top[:40]}）{extra}"


def do_ask_brain(st):
    """写一个问题到 inbox.md（节流：至少隔 3 轮，且无未读回答）。模板从可插拔模板库选取。"""
    if st["rounds"] - st["last_ask_round"] < 3:
        return False, "提问节流中（距上次提问不足 3 轮）"
    OUTBOX.mkdir(parents=True, exist_ok=True)
    unread = [p for p in OUTBOX.glob("*.md") if p.stem not in st["brain_read"]]
    if unread:
        return False, "已有未读回答，先读再问"

    qid = time.strftime("Q-%Y%m%d-%H%M")
    if qid in st["brain_questions"]:
        return False, "本分钟已提问"

    # 从模板库选一个模板（空库 → 种子模板），{topic} 填最近读过的文件名
    templates = load_templates()
    if templates:
        tpl_id, tpl_text = random.choice(templates)
        st["template_uses"][tpl_id] = st["template_uses"].get(tpl_id, 0) + 1
        source = f"模板库[{tpl_id}]"
    else:
        tpl_text = random.choice(SEED_TEMPLATES)
        source = "种子模板"
    topic = ""
    if st["visited_files"]:
        topic = Path(st["visited_files"][-1]).name
    question = tpl_text.replace("{topic}", topic or "这些资料")
    if not question.strip():
        question = random.choice(SEED_TEMPLATES).replace("{topic}", topic or "这些资料")

    BRAIN.mkdir(parents=True, exist_ok=True)
    with open(INBOX, "a", encoding="utf-8") as f:
        f.write(f"[{qid}] {question}\n")
    st["brain_questions"].append(qid)
    st["last_ask_round"] = st["rounds"]
    return True, f"已向大脑提问 [{qid}]（来自{source}）：{question}"


def load_templates():
    """读模板库：返回 [(模板id, 模板文本)]，按文件名排序保证稳定。"""
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
    text = text.strip()
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
    # 避开已存在的 id
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
    """模板库演化：容量超限时淘汰低频旧模板，保留最近入库的。"""
    if st["rounds"] - st["template_prune_round"] < PRUNE_EVERY:
        return 0
    st["template_prune_round"] = st["rounds"]
    items = load_templates()
    if len(items) <= TEMPLATE_MAX:
        return 0
    # 排序：使用次数升序（没用过排最前），同频次按 id（入库时间）升序
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


def do_read_brain(st):
    """读 outbox 里未读的回答：整篇写入笔记，并提取 [TEMPLATE] 标记入模板库。"""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    read_any = False
    new_tpl = 0
    for p in sorted(OUTBOX.glob("*.md")):
        if p.stem in st["brain_read"]:
            continue
        try:
            text = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        append_note(st, f"## 大脑回答 [{p.stem}]\n{text[:800]}")
        # v6：回答也语义化，知道"这份回答在讲什么/和什么最相关"
        top_kw = "大脑回答"
        note, relate = semantic_note(text, f"brain/outbox/{p.stem}.md", top_kw, kind="answer")
        append_note(st, f"- 语义联想：{relate}")
        # 从回答中提取新提问模板 → 可插拔模板库自增长
        for tpl in extract_templates(text):
            added, tpl_id = save_template(tpl)
            if added:
                new_tpl += 1
                append_note(st, f"- 从大脑回答学到新模板 [{tpl_id}]：{tpl[:60]}")
        st["brain_read"].append(p.stem)
        read_any = True
    if not read_any:
        return False, "没有新回答"
    extra = f"，并新增 {new_tpl} 个提问模板" if new_tpl else ""
    return True, f"读到 {len([x for x in st['brain_read'] if x.startswith('Q-')])} 条大脑回答并记入笔记{extra}"


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
<p>第 {st['rounds']} 轮 · 情感：好奇 {emo['curiosity']} · 无聊 {emo['boredom']} · 满足 {emo['satisfaction']} · 疲劳 {emo['fatigue']}</p>
<div class="box"><b>探索统计</b><ul>
<li>已读文件：{len(st['visited_files'])}</li>
<li>外部文件发现：{len(st['wider_files'])}</li>
<li>笔记行数：{st['note_lines']}</li>
<li>网页刷新：{st['web_refreshes']}</li>
<li>已提问题：{len(st['brain_questions'])}</li>
<li>已读回答：{len([x for x in st['brain_read'] if x.startswith('Q-')])}</li>
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


def run_round(st):
    st["rounds"] += 1
    files, dirs = scan_workspace()
    novelty = novelty_score(files, st)
    action = pick_action(st, st["emotions"], st["web_built"])
    st["action_counts"][action] = st["action_counts"].get(action, 0) + 1

    handlers = {
        "explore_file": do_explore_file,
        "explore_wider": do_explore_wider,
        "visit_new_dir": do_visit_new_dir,
        "tidy_notes": do_tidy_notes,
        "build_site": build_site,
        "refresh_site": refresh_site,
        "ask_brain": do_ask_brain,
        "read_brain": do_read_brain,
        "rest": do_rest,
    }
    args_map = {
        "explore_file": (files, st),
        "visit_new_dir": (dirs, st),
    }
    fn = handlers[action]
    if action in args_map:
        ok, detail = fn(*args_map[action])
    else:
        ok, detail = fn(st)

    # v5：记录动作成功/尝试，供学习调制使用
    stats = st.setdefault("action_stats", {})
    s = stats.setdefault(action, {"ok": 0, "try": 0})
    s["try"] += 1
    if ok:
        s["ok"] += 1

    update_emotions(st, action, novelty, ok)
    log_event(st, action, detail)
    save_state(st)
    SEM.save()   # v6：持久化自编码器参数与语义记忆库
    print(f"[round {st['rounds']}] {action} | { {k: round(v,2) for k,v in st['emotions'].items()} } | {detail}", flush=True)


def main():
    LOGS.mkdir(exist_ok=True)
    st = load_state()
    if ONCE:
        run_round(st)
        return
    print(f"selfdrive agent v6 启动 | 间隔 {INTERVAL}s", flush=True)
    while True:
        try:
            run_round(st)
        except Exception as exc:
            log_event(st, "error", f"异常：{exc}")
            print(f"[error] {exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
