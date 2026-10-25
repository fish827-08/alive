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

from . import (config, outcomes, research, rules_compiler, skills, templates,
               teacherless, working_memory)
from .config import LOGS, NOVELTY_WEB_SCALE, ONCE
from .decision import choose_action, evaluate_candidates, pick_actions
from .emotions import apply_energy, update_emotions
from .state import clamp, load_state, log_event, save_state

SEM = sem.Semantics(config.BASE)

# v9.21/v9.22：按安装目录自动识别自己是谁；目录下的 agent_name.txt 优先（用户命名，如"火星"）
AGENT_NAME = None
try:
    AGENT_NAME = (config.BASE / "agent_name.txt").read_text(encoding="utf-8").strip()
except Exception:
    AGENT_NAME = None
if not AGENT_NAME:
    if config.BASE.name == "moon":
        AGENT_NAME = "moon"
    elif config.BASE.name == "selfdrive":
        AGENT_NAME = "huiyue"
    else:
        AGENT_NAME = config.BASE.name


def run_round(st):
    st["rounds"] += 1
    st["round_events"] = []          # ③④：本轮事件标签（每轮清空）
    st.pop("card_boost", None)       # v9.20：清上轮概念卡乘子（每轮重新处境匹配）
    templates.check_template_retirement(st)
    # v9.23.2 收割语言区后台完成的思考（把话说完的接续点）
    try:
        from . import llm_bridge
        llm_bridge.harvest_think(st)
    except Exception:
        pass
    # v9.20 处境匹配：affordance 点亮概念卡 → 卡乘子进 pick_action（使用即学习）
    try:
        from . import conceptcard
        conceptcard.situation_match(st)
    except Exception:
        pass
    # v9.24 阶段 2：从本地缺口刷新研究任务（不联网），再进工作记忆与决策
    try:
        research.ensure_tasks(st)
    except Exception as exc:
        log_event(st, "error", f"研究任务刷新失败：{exc.__class__.__name__}: {exc}")
    files, dirs = skills.scan_workspace()
    novelty = skills.novelty_score(files, st)
    working_memory.build(st)
    from .decision import pick_actions
    picks = pick_actions(st, st["emotions"], st["web_built"])
    blocked = [a for a in picks if not teacherless.allow_action(a)]
    if blocked:
        teacherless.audit(event="selection_blocked", channel=",".join(blocked),
                          detail="决策层试图选择被禁用通道，已在执行前拦截",
                          round=st["rounds"])
        picks = [a for a in picks if teacherless.allow_action(a)]
    if not picks:
        picks = ["rest"]
    action = picks[0]
    side_picks = picks[1:]           # v9.23.2 辅动作：同一轮内轻量并行执行
    candidates = evaluate_candidates(st, {a: 1.0 for a in picks})
    working_memory.attach_decision(st, action, candidates)
    outcomes.begin(st, action, candidates)
    save_state(st)                    # 行动前预期必须先落盘，崩溃后才能识别未结算行动
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
        "sibling_letter": skills.do_sibling_letter,   # v9.21 兄弟信箱
        "self_tune": skills.do_self_tune,             # v9.23 自我修改（白名单）
        "llm_consult": skills.do_llm_consult,         # v9.22 LLM 提案（可插拔）
        "rest": skills.do_rest,
    }
    args_map = {
        "explore_file": (files, st),
        "visit_new_dir": (dirs, st),
        "sibling_letter": (st, AGENT_NAME),
        "self_tune": (st, AGENT_NAME),
        "llm_consult": (st, AGENT_NAME),
    }
    fn = handlers[action]
    try:
        if action in args_map:
            ok, detail = fn(*args_map[action])
        else:
            ok, detail = fn(st)
    except Exception as exc:
        outcomes.settle(st, False, f"{exc.__class__.__name__}: {exc}", forced_status="inconclusive")
        save_state(st)
        raise

    # 成功率统计（P2 修复：read_brain 无新回答的空转不计尝试）
    stats = st.setdefault("action_stats", {})
    idle = (action == "read_brain" and not ok) or (action == "ask_brain" and not ok)
    s = stats.setdefault(action, {"ok": 0, "try": 0})
    if not idle:
        s["try"] += 1
        if ok:
            s["ok"] += 1
    # v9.23.2 辅动作：同轮轻量执行（计数+日志；生理/情绪结算只归主动作）
    for side in side_picks:
        try:
            sfn = handlers[side]
            sargs = args_map.get(side, (st,))
            sok, sdetail = sfn(*sargs)
            st["action_counts"][side] = st["action_counts"].get(side, 0) + 1
            ss = st.setdefault("action_stats", {}).setdefault(side, {"ok": 0, "try": 0})
            sidle = (side == "read_brain" and not sok) or (side == "ask_brain" and not sok)
            if not sidle:
                ss["try"] += 1
                if sok:
                    ss["ok"] += 1
            log_event(st, side, f"[辅] {sdetail}")
        except Exception as exc:
            log_event(st, "error", f"辅动作 {side} 异常：{exc.__class__.__name__}")
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
    outcomes.settle(st, ok, detail)
    log_event(st, action, detail)
    save_state(st)
    SEM.save()
    teacherless.assert_state_clean(st)   # 状态落盘后再验一遍：基线之后不许新增教师/LLM痕迹
    print(f"[round {st['rounds']}] {action} | { {k: round(v, 2) for k, v in st['emotions'].items()} } | {detail}", flush=True)


def main():
    manifest = None
    if config.TEACHERLESS:
        # 启动前先验隔离与种子账本：未初始化/状态不属于本实验/基线被突破 → 拒绝运行
        manifest = teacherless.verify_clean_start()
    LOGS.mkdir(exist_ok=True)
    templates.ensure_builtin_templates()      # ⑤ 相反/先后模板就位
    st = load_state()
    if config.TEACHERLESS:
        teacherless.stamp_state(st, manifest)
        teacherless.assert_state_clean(st, manifest)
        save_state(st)
        teacherless.audit(event="startup", channel="runtime",
                          detail=(f"experiment={manifest['experiment_id']} "
                                  f"seed_policy={manifest['seed_policy']} "
                                  f"rounds={st.get('rounds', 0)} "
                                  f"code={str(manifest.get('code_sha256', ''))[:12]}"))
    elif st.get("teacherless"):
        teacherless.assert_mode_matches_state(st)
    templates.ensure_asked_texts(st)          # 查重窗口回填（升级兼容）
    if ONCE:
        run_round(st)
        return
    print(f"selfdrive agent v9.23（工作记忆/证据门禁/行动后果）启动 | 间隔 {config.INTERVAL}s", flush=True)
    while True:
        try:
            run_round(st)
        except Exception as exc:
            log_event(st, "error", f"异常：{exc.__class__.__name__}: {exc}")
            print(f"[error] {exc.__class__.__name__}: {exc}", flush=True)
            if config.TEACHERLESS:
                raise              # 隔离基线失败要停机暴露，不能带病继续跑
        time.sleep(config.INTERVAL)
