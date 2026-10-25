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
import math
import random

from . import config
from .config import (SKILLS, WEB_SHARE_CAP, load_ask_config)
from .state import web_share
from .tokenizer import FORMAT_NOISE


def legacy_weights(st, e, web_built):
    """旧策略的可解释先验：基础权重乘历史门控，不消耗随机数。"""
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
    # ⑭ LLM 关闭、冷却或预算耗尽时不产生空调用候选
    from . import llm_bridge
    if not llm_bridge.available(st):
        w["llm_consult"] = 0.0
    # ⑮ v9.23 自我调参持久化：self_tune 写入 ask_config 的 self_tuned 段
    #    （"屡屡碰壁"的动作被自己持久降权——自我修改真正改变行为的一环）
    try:
        for full_key, boost in load_ask_config().get("self_tuned", {}).items():
            act, _, kind = full_key.partition(".")
            if kind == "base_boost" and act in w:
                w[act] *= max(0.0, float(boost))
    except Exception:
        pass
    # ⑫ v9.16 web 配额：自食缓存占比超限 → 压制读文件（去外部找吃的）
    if web_share(st) >= WEB_SHARE_CAP:
        w["explore_file"] *= 0.25
        w["web_search"] *= 1.3
    if sum(w.values()) <= 0.0:
        w["rest"] = 1.0
    return w


def success_probability(st, action):
    stats = st.get("action_stats", {}).get(action, {})
    return (stats.get("ok", 0) + 1.0) / (stats.get("try", 0) + 2.0)


def evaluate_candidates(st, weights):
    from .emotions import expected_energy_delta

    info_values = {
        "explore_file": 0.15, "explore_wider": 0.16, "web_search": 0.20,
        "read_brain": 0.14, "self_talk": 0.10, "consolidate_memory": 0.07,
        "reflect_memory": 0.07, "llm_consult": 0.08, "sibling_letter": 0.08,
    }
    risks = {"explore_wider": 0.08, "web_search": 0.05, "build_site": 0.04}
    energy = st.get("energy", 1.0)
    target = 0.8
    candidates = []
    for action, prior in weights.items():
        if prior <= 0.0:
            continue
        probability = success_probability(st, action)
        success_delta = expected_energy_delta(action, True)
        failure_delta = expected_energy_delta(action, False)
        energy_delta = probability * success_delta + (1.0 - probability) * failure_delta
        before_gap = abs(energy - target)
        after_gap = abs(max(0.0, min(1.0, energy + energy_delta)) - target)
        homeostasis = before_gap - after_gap
        information = info_values.get(action, 0.02) * (1.0 - 0.5 * probability)
        risk = risks.get(action, 0.0)
        model = st.get("outcome_stats", {}).get(action, {})
        calibration_penalty = 0.15 * model.get("brier", 0.0)
        utility = (math.log1p(max(0.0, prior)) + 0.4 * probability
                   + 12.0 * homeostasis + information - risk - calibration_penalty)
        candidates.append({
            "action": action,
            "prior_weight": round(prior, 6),
            "success_probability": round(probability, 6),
            "expected_energy_delta": round(energy_delta, 6),
            "homeostasis_gain": round(homeostasis, 6),
            "information_value": round(information, 6),
            "risk": risk,
            "utility": round(utility, 6),
        })
    return candidates


def choose_action(st, e, web_built):
    weights = legacy_weights(st, e, web_built)
    candidates = evaluate_candidates(st, weights)
    st["decision_mode"] = config.DECISION_MODE
    if config.DECISION_MODE in ("legacy", "shadow"):
        names = list(weights)
        action = random.choices(names, weights=[weights[n] for n in names], k=1)[0]
    else:
        ranked = sorted(candidates, key=lambda c: c["utility"], reverse=True)
        best = ranked[0]["utility"]
        shortlist = [c for c in ranked if best - c["utility"] <= 0.6][:4]
        scores = [math.exp(c["utility"] - best) for c in shortlist]
        action = random.choices([c["action"] for c in shortlist], weights=scores, k=1)[0]
    for candidate in candidates:
        candidate["selected"] = candidate["action"] == action
    return action, candidates




def _weights_snapshot(st, e, web_built):
    """辅动作抽签用的权重快照：完整走一遍 legacy 门控（含 [RULE]/概念卡/自调参乘子）。"""
    return legacy_weights(st, e, web_built)

def pick_action(st, e, web_built):
    return choose_action(st, e, web_built)[0]


# v9.23.2 一轮多技能：主动作之外按权重补选辅动作；互斥对不同轮
MUTEX_PAIRS = {
    ("ask_brain", "read_brain"),      # 有未读时抑制提问，逻辑互斥
    ("explore_file", "rest"),         # 走和睡矛盾
    ("build_site", "refresh_site"),   # 同站点二选一
    ("self_talk", "sibling_letter"),  # 内部言语与对外通信不混轮
    ("web_search", "read_brain"),
}


def pick_actions(st, e, web_built, extra=None):
    """一轮多技能：返回 [主动作, *辅动作]。辅动作按剩余权重抽，避开互斥与重复。"""
    import random as _r
    main = pick_action(st, e, web_built)
    extra = config.MULTI_SKILL_COUNT if extra is None else extra
    picks = [main]
    stats = st.get("action_stats", {})
    for _ in range(extra):
        w2 = {a: v for a, v in _current_weights(st, e, web_built).items()
              if a not in picks
              and not any((a, p) in MUTEX_PAIRS or (p, a) in MUTEX_PAIRS for p in picks)}
        # 辅动作沿用主循环的失败抑制（简单版：零成功老动作不再辅选）
        for a in list(w2):
            s = stats.get(a, {"ok": 0, "try": 0})
            if s.get("try", 0) >= 15 and s.get("ok", 0) == 0:
                w2.pop(a)
        if not w2 or sum(w2.values()) <= 0:
            break
        picks.append(_r.choices(list(w2), weights=list(w2.values()), k=1)[0])
    return picks


def _current_weights(st, e, web_built):
    """重建权重表（供辅动作抽签）：直接复用 pick_action 的完整门控。"""
    import random as _r2
    return _weights_snapshot(st, e, web_built)


def explore_starved(st):
    """探索枯竭检测：最近探索频频扑空（成功率 <20% 且尝试 ≥4 次）。"""
    stats = st.get("action_stats", {})
    s = stats.get("explore_file", {"ok": 0, "try": 0})
    wd = stats.get("explore_wider", {"ok": 0, "try": 0})
    tot, okc = s["try"] + wd["try"], s["ok"] + wd["ok"]
    return tot >= 4 and (okc / tot) < 0.2
