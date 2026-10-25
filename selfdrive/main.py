# -*- coding: utf-8 -*-
"""main — 主循环（v9.16 拆出）。

每轮编排：清事件 → 扫描 → 决策 → 执行技能 → 能量/成功率 → 情感演化
（新意效率：读自缓存时增益打折④）→ [RULE] 轮末触发③ → 感受队列结算④
→ 情绪奖赏表 → 日志/持久化。
"""
import sys
import time

try:
    import semantics as sem
except ImportError:
    from . import config as _cfg
    sys.path.insert(0, str(_cfg.BASE))
    import semantics as sem

from . import config, rules_compiler, skills, templates
from .config import LOGS, NOVELTY_WEB_SCALE, ONCE
from .decision import pick_action
from .emotions import apply_energy, update_emotions
from .state import clamp, load_state, log_event, save_state

SEM = sem.Semantics(config.BASE)


def run_round(st):
    st["rounds"] += 1
    st["round_events"] = []          # ③④：本轮事件标签（每轮清空）
    st.pop("card_boost", None)       # v9.20：清上轮概念卡乘子（每轮重新处境匹配）
    templates.check_template_retirement(st)
    # v9.20 处境匹配：affordance 点亮概念卡 → 卡乘子进 pick_action（使用即学习）
    try:
        from . import conceptcard
        conceptcard.situation_match(st)
    except Exception:
        pass
    files, dirs = skills.scan_workspace()
    novelty = skills.novelty_score(files, st)
    action = pick_action(st, st["emotions"], st["web_built"])
    st["action_counts"][action] = st["action_counts"].get(action, 0) + 1
    # 渴求度更新（v9.8b）
    urge_cfg = config.load_ask_config().get("urge", {})
    if urge_cfg.get("enabled", True):
        inc = urge_cfg.get("increment", 1.0)
        cap = urge_cfg.get("cap", 20)
        st.setdefault("urge", {})
        for a in urge_cfg.get("actions", ["reflect_memory", "consolidate_memory"]):
            if a == action:
                st["urge"][a] = 0
            else:
                st["urge"][a] = min(cap, st["urge"].get(a, 0) + inc)

    handlers = {
        "explore_file": skills.do_explore_file,
        "explore_wider": skills.do_explore_wider,
        "visit_new_dir": skills.do_visit_new_dir,
        "tidy_notes": skills.do_tidy_notes,
        "build_site": skills.build_site,
        "refresh_site": skills.refresh_site,
        "ask_brain": skills.do_ask_brain,
        "read_brain": skills.do_read_brain,
        "web_search": skills.do_web_search,
        "consolidate_memory": skills.do_consolidate_memory,
        "reflect_memory": skills.do_reflect_memory,
        "self_talk": skills.do_self_talk,
        "rest": skills.do_rest,
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

    # 成功率统计（P2 修复：read_brain 无新回答的空转不计尝试）
    stats = st.setdefault("action_stats", {})
    idle = (action == "read_brain" and not ok) or (action == "ask_brain" and not ok)
    s = stats.setdefault(action, {"ok": 0, "try": 0})
    if not idle:
        s["try"] += 1
        if ok:
            s["ok"] += 1
    apply_energy(st, action, ok)

    # 能力里程碑（v9.5）
    comp_triggered = False
    rate = 0.0
    if ok and s["try"]:
        rate = s["ok"] / s["try"]
        cl = st.setdefault("competence", {})
        if rate >= 0.5 and rate > cl.get(action, 0.0):
            cl[action] = rate
            comp_triggered = True

    # 情感演化（④：新意效率——读自缓存的成功增益打折；技能执行时写入 last_read_kind）
    old_e = dict(st["emotions"])
    read_kind = st.pop("last_read_kind", "")
    novelty_eff = novelty * (NOVELTY_WEB_SCALE if read_kind == "web" else 1.0)
    update_emotions(st, action, novelty, ok, novelty_eff=novelty_eff)
    if comp_triggered:
        st["emotions"]["satisfaction"] = clamp(st["emotions"]["satisfaction"] + 0.1, 0.0, 1.0)
        st["emotions"]["curiosity"] = clamp(st["emotions"]["curiosity"] + 0.05, 0.0, 1.0)
        detail = detail + f"｜能力里程碑：{action} 成功率 {rate:.0%}"

    # ③ [RULE] 轮末触发（其情绪效果进感受队列）
    rules_compiler.apply_round_rules(st)
    # ④ 感受队列结算（痛感/快感一次性增量）
    e = st["emotions"]
    for dim, delta in st.pop("feelings", []):
        e[dim] = clamp(e.get(dim, 0.5) + delta, 0.0, 1.0)

    # 情绪奖赏表（v9.4）
    new_e = st["emotions"]
    mr = st.setdefault("mood_reward", {})
    m = mr.setdefault(action, {"sat_sum": 0.0, "cur_sum": 0.0, "n": 0})
    m["sat_sum"] += new_e.get("satisfaction", 0.0) - old_e.get("satisfaction", 0.0)
    m["cur_sum"] += new_e.get("curiosity", 0.0) - old_e.get("curiosity", 0.0)
    m["n"] += 1
    log_event(st, action, detail)
    save_state(st)
    SEM.save()
    print(f"[round {st['rounds']}] {action} | { {k: round(v, 2) for k, v in st['emotions'].items()} } | {detail}", flush=True)


def main():
    LOGS.mkdir(exist_ok=True)
    templates.ensure_builtin_templates()      # ⑤ 相反/先后模板就位
    st = load_state()
    templates.ensure_asked_texts(st)          # 查重窗口回填（升级兼容）
    if ONCE:
        run_round(st)
        return
    print(f"selfdrive agent v9.18（模块化·扩散激活/做梦/分层图）启动 | 间隔 {config.INTERVAL}s", flush=True)
    while True:
        try:
            run_round(st)
        except Exception as exc:
            log_event(st, "error", f"异常：{exc.__class__.__name__}: {exc}")
            print(f"[error] {exc.__class__.__name__}: {exc}", flush=True)
        time.sleep(config.INTERVAL)
