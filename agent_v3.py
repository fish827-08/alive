#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfdrive agent v3 — 新增"大脑桥接"：每小时可向大脑（Claude）提问一次
  - ask_brain：好奇高时写一个问题到 brain/inbox.md（每轮最多 1 个，至少隔 3 轮）
  - read_brain：检查 brain/outbox/ 的新回答，读入笔记
文件约定：
  ~/selfdrive/brain/inbox.md        agent 追加问题：[Q-编号] 问题内容
  ~/selfdrive/brain/outbox/Q-编号.md  大脑回答（文件名 = 问题编号）
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
LOGS = BASE / "logs"
STATE_FILE = BASE / "state.json"
INTERVAL = int(os.environ.get("SELFDRIVE_INTERVAL", "300"))
ONCE = "--once" in sys.argv

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


def pick_action(e, web_built):
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
    names = list(w.keys())
    return random.choices(names, weights=[w[n] for n in names], k=1)[0]


def read_head(path, n=8):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return [l.rstrip() for l in f.readlines()[:n]]
    except Exception:
        return []


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
    f = random.choice(pool)
    lines = read_head(BASE / f["path"])
    title = lines[0][:60] if lines else f["name"]
    words = Counter(re.findall(r"[\u4e00-\u9fa5]{2,}", " ".join(lines)))
    top = "、".join(w for w, _ in words.most_common(5)) or "无"
    append_note(st, f"## 读到：{f['name']}\n- 首行：{title}\n- 关键词：{top}")
    if f["path"] not in st["visited_files"]:
        st["visited_files"].append(f["path"])
    return True, f"读了 {f['name']}（{len(lines)} 行，关键词：{top}）"


def do_explore_wider(st):
    found = scan_wider(st)
    if not found:
        return False, "更大范围没有新文本文件"
    key = random.choice(found)
    try:
        lines = read_head(key, 8)
        words = Counter(re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{4,}", " ".join(lines)))
        top = "、".join(w for w, _ in words.most_common(5)) or "无"
        append_note(st, f"## 探到外部文件：{key}\n- 首行：{lines[0][:60] if lines else ''}\n- 关键词：{top}")
        return True, f"读了外部文件 {Path(key).name}（{len(lines)} 行，关键词：{top}）"
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
    return True, f"整理笔记（高频词：{top[:40]}）"


def do_ask_brain(st):
    """写一个问题到 inbox.md（节流：至少隔 3 轮，且无未读回答）。"""
    if st["rounds"] - st["last_ask_round"] < 3:
        return False, "提问节流中（距上次提问不足 3 轮）"
    OUTBOX.mkdir(parents=True, exist_ok=True)
    unread = [p for p in OUTBOX.glob("*.md") if p.stem not in st["brain_read"]]
    if unread:
        return False, "已有未读回答，先读再问"

    qid = time.strftime("Q-%Y%m%d-%H%M")
    if qid in st["brain_questions"]:
        return False, "本分钟已提问"

    # 从读过的文件里挑一个当话题，生成问题
    topic = ""
    if st["visited_files"]:
        topic = Path(st["visited_files"][-1]).name
    candidates = [
        f"我读了《{topic}》，它的核心意思是什么？有什么我没注意到的要点？",
        "我探索了这些资料（人工生命、信息论、天气），它们之间有什么联系？",
        "除了我现在读的这些东西，还有哪些值得探索的方向？",
        "我现在应该尝试做什么新的事情？",
    ]
    question = random.choice(candidates)
    BRAIN.mkdir(parents=True, exist_ok=True)
    with open(INBOX, "a", encoding="utf-8") as f:
        f.write(f"[{qid}] {question}\n")
    st["brain_questions"].append(qid)
    st["last_ask_round"] = st["rounds"]
    return True, f"已向大脑提问 [{qid}]：{question}"


def do_read_brain(st):
    """读 outbox 里未读的回答，写入笔记。"""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    read_any = False
    for p in sorted(OUTBOX.glob("*.md")):
        if p.stem in st["brain_read"]:
            continue
        try:
            text = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        append_note(st, f"## 大脑回答 [{p.stem}]\n{text[:800]}")
        st["brain_read"].append(p.stem)
        read_any = True
    if not read_any:
        return False, "没有新回答"
    return True, f"读到 {len([x for x in st['brain_read'] if x.startswith('Q-')])} 条大脑回答并记入笔记"


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
    action = pick_action(st["emotions"], st["web_built"])
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

    update_emotions(st, action, novelty, ok)
    log_event(st, action, detail)
    save_state(st)
    print(f"[round {st['rounds']}] {action} | { {k: round(v,2) for k,v in st['emotions'].items()} } | {detail}", flush=True)


def main():
    LOGS.mkdir(exist_ok=True)
    st = load_state()
    if ONCE:
        run_round(st)
        return
    print(f"selfdrive agent v3 启动 | 间隔 {INTERVAL}s", flush=True)
    while True:
        try:
            run_round(st)
        except Exception as exc:
            log_event(st, "error", f"异常：{exc}")
            print(f"[error] {exc}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
