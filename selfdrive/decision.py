# -*- coding: utf-8 -*-
"""decision — 决策门控 pick_action（v9.16 拆出并加强）。

权重管线（在 v9 十道门基础上）：
  ① 学习调制（成功率）  v9.16：0 成功且 ≥20 次尝试 → ×0.15（结构性死动作强降权）
  ② 提问饥饿  ③ 未读回答优先读 / 无未读时强抑 read（修"157 轮空转"）
  ④ 探索枯竭切换  ⑤ web 搜索冷却  ⑥ 情绪奖赏 Q-table
  ⑦ 昼夜节律  ⑧ 能量内稳态  ⑨ 渴求度  ⑩ 学习进展 LP
  ⑪ v9.16 [RULE] 编译乘子（陈述→动作的落地处）
  ⑫ v9.16 web 配额：自食缓存占比 ≥ 上限 → 压制 explore_file（配合选文件过滤）
"""
import random

from . import config
from .config import (SKILLS, WEB_SHARE_CAP, load_ask_config)
from .state import web_share
from .tokenizer import FORMAT_NOISE


def pick_action(st, e, web_built):
    """权重 = base + Σ(技能对情感维度的系数 × 情感当前值)，再过十二道门。"""
    w = {}
    for name, sk in SKILLS.items():
        wd = sk.get("weight", {})
        val = wd.get("base", 0.0)
        for key, coef in wd.items():
            if key == "base":
                continue
            if key == "curiosity_x_boredom":
                val += coef * e.get("curiosity", 0.0) * e.get("boredom", 0.0)
            else:
                val += coef * e.get(key, 0.0)
        if name == "build_site" and not web_built:
            val += sk.get("bonus_not_built", 0.5)
        elif name == "refresh_site" and not web_built:
            val = wd.get("base", 0.05)
        w[name] = max(0.0, val)
    # ① 学习调制：动作历史成功率动态调权重
    stats = st.get("action_stats", {})
    for a in w:
        s = stats.get(a, {"ok": 0, "try": 0})
        if s["try"] >= 3:
            rate = s["ok"] / s["try"]
            if rate >= 0.5:
                w[a] *= 1.3
            elif rate <= 0.25 and s["try"] >= 6:
                w[a] *= 0.6
        if s["try"] >= 20 and s["ok"] == 0:          # v9.16：结构性死动作强降权
            w[a] *= 0.15
    # ② 提问饥饿
    hunger = min(1.0, (st["rounds"] - st["last_ask_round"]) / 6.0)
    w["ask_brain"] *= 1.0 + hunger * 1.5
    if not st["brain_questions"]:
        w["ask_brain"] += 0.5
    # ③ 有未读回答优先读；无未读强抑（修"read_brain 空转"）
    unread = []
    if config.OUTBOX.exists():
        unread = [p for p in config.OUTBOX.glob("*.md") if p.stem not in st["brain_read"]]
    if unread:
        w["read_brain"] *= 1.0 + min(2.0, len(unread) * 1.2)
        w["ask_brain"] *= 0.4
    else:
        w["read_brain"] *= 0.15
    # ④ 探索枯竭时压制无效探索、抬升求知
    if explore_starved(st):
        for a in ("explore_file", "explore_wider", "visit_new_dir"):
            w[a] *= 0.5
        w["ask_brain"] *= 1.4
    # ⑤ web_search 冷却
    ws = load_ask_config().get("web_search", {})
    ws_cd = ws.get("cooldown_rounds", 40)
    last_ws = st.get("last_web_search_round", -999)
    if (st["rounds"] - last_ws) < ws_cd:
        w["web_search"] *= 0.0
    # ⑥ 情绪奖赏（Q-table 雏形）
    mr = st.get("mood_reward", {})
    for a in w:
        m = mr.get(a)
        if m and m["n"] >= 5:
            avg = (m["sat_sum"] + m["cur_sum"]) / m["n"]
            if avg > 0.05:
                w[a] *= 1.0 + min(0.3, avg)
            elif avg < -0.05:
                w[a] *= max(0.5, 1.0 - min(0.25, -avg))
    # ⑦ 昼夜节律
    from .emotions import day_factor
    df = day_factor()
    for a in ("explore_file", "explore_wider", "visit_new_dir", "web_search", "ask_brain", "build_site", "refresh_site"):
        w[a] *= df
    for a in ("rest", "tidy_notes", "consolidate_memory", "reflect_memory"):
        w[a] *= 1.5 - df
    # ⑧ 能量内稳态
    en = st.get("energy", 1.0)
    if en < 0.35:
        w["explore_file"] *= 1.6
        w["ask_brain"] *= 1.5
        w["rest"] *= 1.8
        w["tidy_notes"] *= 0.4
        w["refresh_site"] *= 0.4
    # ⑨ 渴求度
    urge_cfg = load_ask_config().get("urge", {})
    if urge_cfg.get("enabled", True):
        for a in urge_cfg.get("actions", ["reflect_memory", "consolidate_memory"]):
            u = st.get("urge", {}).get(a, 0)
            if u <= 0:
                continue
            th = urge_cfg.get("double_threshold", 6)
            if u >= th:
                w[a] *= (urge_cfg.get("double_base", 1.5) ** u)
            else:
                w[a] *= (1.0 + u * urge_cfg.get("linear_factor", 0.2))
    # ⑩ 学习进展 LP
    if st.get("lp"):
        _latest = sorted(st["lp"].values(), key=lambda x: -x.get("last_round", 0))[0]
        if len(_latest.get("err_history", [])) >= 2:
            _lp = _latest.get("lp", 0.0)
            if _lp > 0.05:
                w["explore_file"] *= 1.2
                w["explore_wider"] *= 1.1
            elif _lp < -0.05:
                w["ask_brain"] *= 1.2
                w["web_search"] *= 1.1
    # ⑪ v9.16 [RULE] 编译乘子（陈述→动作）
    for a, f in st.get("skill_multipliers", {}).items():
        if a in w:
            w[a] *= max(0.0, f)
    # ⑬ v9.20 概念卡乘子（处境匹配点亮概念、概念影响决策——使用即学习）
    for a, f in st.pop("card_boost", {}).items():
        if a in w:
            w[a] *= max(0.0, f)
    # ⑫ v9.16 web 配额：自食缓存占比超限 → 压制读文件（去外部找吃的）
    if web_share(st) >= WEB_SHARE_CAP:
        w["explore_file"] *= 0.25
        w["web_search"] *= 1.3
    names = list(w.keys())
    return random.choices(names, weights=[w[n] for n in names], k=1)[0]


def explore_starved(st):
    """探索枯竭检测：最近探索频频扑空（成功率 <20% 且尝试 ≥4 次）。"""
    stats = st.get("action_stats", {})
    s = stats.get("explore_file", {"ok": 0, "try": 0})
    wd = stats.get("explore_wider", {"ok": 0, "try": 0})
    tot, okc = s["try"] + wd["try"], s["ok"] + wd["ok"]
    return tot >= 4 and (okc / tot) < 0.2
