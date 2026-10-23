#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfdrive agent — 云服务器自驱力实验（Phase A：无 LLM 纯脚本小脑）
一个在沙盒文件系统里"生活"的 agent：
  观察 → 新奇度评分 → 情感更新 → 选动作 → 执行 → 日志
只使用 Python 标准库；只在 WORKSPACE 内活动（安全底线）。
"""
import json
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path

# ---------- 配置（可改） ----------
BASE = Path(__file__).resolve().parent
WORKSPACE = BASE / "workspace"           # 沙盒：agent 只能动这里
ARCHIVE = WORKSPACE / "archive"          # 资料区（只读口粮）
NOTES = WORKSPACE / "notes"              # 笔记区（agent 的输出）
LOGS = BASE / "logs"
STATE_FILE = BASE / "state.json"         # 情感/访问记录持久化
INTERVAL = int(os.environ.get("SELFDRIVE_INTERVAL", "300"))  # 轮间隔秒
ONCE = "--once" in sys.argv

EMOTIONS = ["curiosity", "boredom", "satisfaction", "fatigue"]
ACTIONS = ["explore_file", "visit_new_dir", "tidy_notes", "rest"]

# ---------- 状态 ----------
def default_state():
    return {
        "emotions": {"curiosity": 0.3, "boredom": 0.1, "satisfaction": 0.2, "fatigue": 0.0},
        "visited_files": [],          # 已读文件相对路径
        "visited_dirs": [],           # 已访问目录相对路径
        "action_counts": {},          # 动作历史（算行为熵）
        "note_lines": 0,
        "rounds": 0,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return default_state()

def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1))

def log_event(st, action, detail):
    LOGS.mkdir(exist_ok=True)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "round": st["rounds"],
        "emotions": {k: round(v, 3) for k, v in st["emotions"].items()},
        "action": action,
        "detail": detail,
    }
    with open(LOGS / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

# ---------- 观察与新奇度 ----------
def scan():
    """扫描沙盒：返回文件清单、目录清单、统计。"""
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

def novelty_score(files, st):
    """新奇度：新文件占比 + 未访问占比 + 新mtime。"""
    if not files:
        return 0.0
    total = len(files)
    newish = sum(1 for f in files if time.time() - f["mtime"] < 7 * 86400)
    unseen = sum(1 for f in files if f["path"] not in st["visited_files"])
    return 0.5 * (newish / total) + 0.5 * (unseen / total)

# ---------- 情感动力学 ----------
def update_emotions(st, action, novelty, action_success):
    e = st["emotions"]
    # 好奇：新奇度高→升；连续探索→略降
    e["curiosity"] = clamp(e["curiosity"] + 0.12 * novelty - 0.04)
    # 无聊：重复动作→升；换新领域→降
    e["boredom"] = clamp(e["boredom"] + (0.06 if action in ("explore_file", "tidy_notes") else -0.08) - 0.02)
    # 满足：成功动作→升，缓慢衰减
    e["satisfaction"] = clamp(e["satisfaction"] + 0.15 * action_success - 0.03)
    # 疲劳：活动→升；休息→大降
    e["fatigue"] = clamp(e["fatigue"] + (0.05 if action != "rest" else -0.35))
    # 好奇长期低 → 无聊缓慢接管（autonomous boredom 的雏形）
    if e["curiosity"] < 0.2:
        e["boredom"] = clamp(e["boredom"] + 0.05)
    return e

def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

# ---------- 动作选择 ----------
def pick_action(e):
    w = {
        "explore_file": e["curiosity"] * (1 - e["boredom"]) + 0.2,
        "visit_new_dir": e["curiosity"] * 0.8 + 0.1,
        "tidy_notes": e["satisfaction"] * 0.5 + e["boredom"] * 0.6 + 0.1,
        "rest": e["fatigue"] * 2.0 + (1 - e["curiosity"]) * 0.2,
    }
    names = list(w.keys())
    weights = [w[n] for n in names]
    return random.choices(names, weights=weights, k=1)[0]

# ---------- 动作执行 ----------
def read_head(path, n=8):
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            lines = [l.rstrip() for l in f.readlines()[:n]]
        return lines
    except Exception:
        return []

def append_note(st, text):
    NOTES.mkdir(parents=True, exist_ok=True)
    with open(NOTES / "journey.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")
    st["note_lines"] += 1

def do_explore_file(files, st):
    """挑一个高新奇度文件读，写笔记。"""
    unseen = [f for f in files if f["path"] not in st["visited_files"]]
    pool = unseen if unseen else files
    if not pool:
        return False, "无文件可读"
    f = random.choice(pool)
    lines = read_head(BASE / f["path"])
    title = lines[0][:60] if lines else f["name"]
    words = Counter(re.findall(r"[\u4e00-\u9fa5]{2,}", " ".join(lines)))
    top = "、".join(w for w, _ in words.most_common(5)) or "无"
    append_note(st, f"## 读到：{f['name']}\n- 首行：{title}\n- 行数：{len(lines)}\n- 关键词：{top}")
    if f["path"] not in st["visited_files"]:
        st["visited_files"].append(f["path"])
    return True, f"读了 {f['name']}（{len(lines)} 行，关键词：{top}）"

def do_visit_new_dir(dirs, st):
    """去一个没去过的目录看一眼。"""
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
    """整理笔记区：统计词频 + 归档清单。"""
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

def do_rest(st):
    return True, "休息一轮（疲劳下降）"

# ---------- 指标 ----------
def action_entropy(st):
    counts = st["action_counts"]
    total = sum(counts.values()) or 1
    h = -sum((c / total) * (c / total and __import__("math").log(c / total)) for c in counts.values())
    return h

# ---------- 主循环 ----------
def run_round(st):
    st["rounds"] += 1
    files, dirs = scan()
    novelty = novelty_score(files, st)
    action = pick_action(st["emotions"])
    st["action_counts"][action] = st["action_counts"].get(action, 0) + 1

    if action == "explore_file":
        ok, detail = do_explore_file(files, st)
    elif action == "visit_new_dir":
        ok, detail = do_visit_new_dir(dirs, st)
    elif action == "tidy_notes":
        ok, detail = do_tidy_notes(st)
    else:
        ok, detail = do_rest(st)

    update_emotions(st, action, novelty, ok)
    log_event(st, action, detail)
    save_state(st)
    print(f"[round {st['rounds']}] {action} | 情感={ {k: round(v,2) for k,v in st['emotions'].items()} } | {detail}")

def main():
    LOGS.mkdir(exist_ok=True)
    st = load_state()
    if ONCE:
        run_round(st)
        return
    print(f"selfdrive agent 启动 | 间隔 {INTERVAL}s | 沙盒 {WORKSPACE}")
    while True:
        try:
            run_round(st)
        except Exception as exc:
            log_event(st, "error", f"异常：{exc}")
            print(f"[error] {exc}")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
