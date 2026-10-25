# -*- coding: utf-8 -*-
"""emotions — 情感演化（参数化）/ 昼夜节律 / 能量内稳态（v9.16 拆出）。

v9.16 ④ 痛感：update_emotions 的 success_gain 按新意效率缩放——
重复读自缓存的动作成功也不再白涨 satisfaction（情绪脱离饱和的关键一条）。
一次性的痛感（重复提问/预测失误）由技能层经 state.feel 排队，主循环结算。
"""
import json
import time

from .config import DEFAULT_EMOTIONS_CFG, EMOTIONS_CFG_FILE
from .state import clamp


def load_emotions_cfg():
    """读情感配置（brain/emotions.json）；不存在则写默认。"""
    if not EMOTIONS_CFG_FILE.exists():
        try:
            EMOTIONS_CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
            EMOTIONS_CFG_FILE.write_text(
                json.dumps(DEFAULT_EMOTIONS_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return dict(DEFAULT_EMOTIONS_CFG)
    try:
        data = json.loads(EMOTIONS_CFG_FILE.read_text(encoding="utf-8"))
        return {k: {**dict(DEFAULT_EMOTIONS_CFG.get(k, {})), **v} for k, v in data.items()}
    except Exception:
        return dict(DEFAULT_EMOTIONS_CFG)


def emotion_names():
    return list(load_emotions_cfg().keys())


def update_emotions(st, action, novelty, success, novelty_eff=None):
    """参数化情感更新（v9 规则字段驱动）。v9.16：success_gain × 新意效率。
    novelty_eff：本轮有效新意（读自缓存时被压缩），None 时用 novelty。"""
    e = st["emotions"]
    eff = novelty if novelty_eff is None else novelty_eff
    cfg = load_emotions_cfg()
    for ename, rule in cfg.items():
        cur = e.get(ename, rule.get("default", 0.0))
        if "decay" in rule:
            cur -= rule["decay"]
        if "novelty_gain" in rule:
            cur += rule["novelty_gain"] * novelty
        if "success_gain" in rule:
            cur += rule["success_gain"] * success * max(0.0, min(1.0, eff))   # ④ 增益随新意缩放
        if "grow" in rule:
            cur += rule["grow"].get(action, 0.0)
        if "shrink" in rule:
            cur -= rule["shrink"].get(action, 0.0)
        if "shrink_other" in rule and action not in rule.get("grow", {}):
            cur -= rule["shrink_other"]
        if "extra_grow_curiosity_low" in rule and e.get("curiosity", 0) < 0.2:
            cur += rule["extra_grow_curiosity_low"]
        if "non_rest_gain" in rule and action != "rest":
            cur += rule["non_rest_gain"]
        if "rest_change" in rule and action == "rest":
            cur += rule["rest_change"]
        e[ename] = clamp(cur, 0.0, 1.0)
    return e


def day_factor():
    """昼夜节律相位：6:00-20:00 为白天（1.0），夜间 0.4。"""
    h = time.localtime().tm_hour
    return 1.0 if 6 <= h < 20 else 0.4


BASE_METABOLISM = 0.008
ENERGY_GAINS = {
    "explore_file": 0.06, "explore_wider": 0.06, "read_brain": 0.05,
    "web_search": 0.05, "consolidate_memory": 0.03, "rest": 0.08,
    "reflect_memory": 0.02, "ask_brain": -0.02, "tidy_notes": -0.01,
    "refresh_site": -0.01,
}


def expected_energy_delta(action, ok):
    return -BASE_METABOLISM + (ENERGY_GAINS.get(action, 0.0) if ok else 0.0)


def apply_energy(st, action, ok):
    """内稳态：动作耗能/回能，能量低→恐惧上升（求生动机，不会死亡）。"""
    en = st.setdefault("energy", 1.0)
    en += expected_energy_delta(action, ok)
    st["energy"] = max(0.0, min(1.0, en))
    e = st.setdefault("emotions", {})
    fear = e.get("fear", 0.0)
    if st["energy"] < 0.35:
        fear = min(1.0, fear + 0.12)   # 饿→怕→被迫行动
    elif st["energy"] > 0.7:
        fear = max(0.0, fear - 0.05)
    e["fear"] = round(fear, 4)
