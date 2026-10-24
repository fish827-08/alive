# -*- coding: utf-8 -*-
"""state — 状态读写 / 事件日志 / 笔记追加 / 轮事件与感受队列（v9.16 拆出）。

v9.16 新增状态键（旧 state.json 自动兼容：缺省补默认，已有键原样保留）：
  word_cands        概念候选 {词: {n, srcs, first, last}}（晋升制：词汇靠使用挣来）
  edge_preds        可失败预测 [{a, b, round, deadline, status}]
  compiled_rules    [RULE] 编译出的规则 {rid: {trigger, effects, born_round, source, fires}}
  skill_multipliers 编译规则设置的技能权重乘子（决策层 ⑪ 应用）
  rule_metrics      规则出生时快照（"听懂测试"基线：重复率等）
  asked_texts       最近提问文本（查重用）
  asks_total / dup_asks   提问与重复计数（重复率指标）
  recent_reads      阅读来源滑窗（① web 配额统计）
  q_rel_tags        问题→关系类型标记（相反/先后问题学到的关系边打类型）
  round_events      本轮事件标签（③ 触发 / ④ 钩子；每轮清空）
  feelings          情绪增量队列（本技能层产生，主循环结算时应用）
"""
import json
import time

from . import config
from .config import LOGS, NOTES, STATE_FILE


def default_state():
    from .emotions import load_emotions_cfg   # 延迟导入避免环
    emotions = {}
    for ename, cfg in load_emotions_cfg().items():
        emotions[ename] = cfg.get("default", 0.0)
    return {
        "emotions": emotions,
        "visited_files": [], "visited_dirs": [], "wider_files": {},
        "action_counts": {}, "note_lines": 0, "rounds": 0,
        "web_built": False, "web_refreshes": 0,
        "brain_questions": [], "brain_read": [], "last_ask_round": 0,
        "template_uses": {}, "template_prune_round": 0,
        "action_stats": {},
        "open_asks": 0, "last_open_ask_round": -999, "open_streak": 0,
        "mood_reward": {}, "competence": {},
        "template_failures": {}, "tpl_pending": {},
        "last_kw": "",
        "energy": 1.0,
        "concepts": {},        # {概念: {"n": 次数, "last": 轮}}（v9.16 起晋升制入库）
        "relations": {},       # {"A|B": {"w", "n", "last_touch", "type"?}}
        "rules": {},           # {"A∧B→C": {premises, conclusion, support, confidence, status}}
        "infer_log": [], "last_infer_round": -999,
        "consolidate_round": 0,
        "reflect_log": [], "last_reflect_round": -999,
        "urge": {},
        "pred_log": [],
        "hierarchy": {},
        "transforms": {},
        "lp": {},
        "mistakes": [],
        "pred_layers": [],
        "self_talk_log": [],
        # ---- v9.16 新增 ----
        "word_cands": {},
        "edge_preds": [],
        "compiled_rules": {},
        "skill_multipliers": {},
        "rule_metrics": {},
        "asked_texts": [],
        "asks_total": 0,
        "dup_asks": 0,
        "recent_reads": [],
        "q_rel_tags": {},
        # ---- v9.16.1 ----
        "last_dup_pain_round": -999,   # 查重痛感限频（3轮最多痛一次）
        "last_search_topics": [],      # 最近搜索词（避免同词复读）
        "recent_replay": [],           # 最近重放过的笔记行（重放间隔，别对一行反复回味）
        "q_self_guess": {},            # 自问自答（青梧路径5）：qid → 提问前押的私人猜测
        "selfqa": {"hit": 0, "miss": 0},   # 自问自答战绩
        # ---- v9.18 ----
        "sa_winner_log": [],           # 一层：扩散激活赢家记录 [{"round","path","score"}]
        "recent_active": [],           # 三层 priming：最近激活的概念（做梦起点必须先被点亮）
        "dream_log": [],               # 三层：梦游记录 [{"round","path","bet":[a,b]}]
        "abstract": {},                # 二层：抽象节点 {名: {"members":[...], "born": 轮, "support": n}}
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def load_state():
    d = default_state()
    if STATE_FILE.exists():
        try:
            old = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            for k in d:
                if k in old:
                    d[k] = old[k]
        except Exception:
            pass
    return d


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def log_event(st, action, detail):
    LOGS.mkdir(exist_ok=True)
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "round": st["rounds"],
           "emotions": {k: round(v, 3) for k, v in st["emotions"].items()},
           "action": action, "detail": detail}
    with open(LOGS / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_note(st, text):
    NOTES.mkdir(parents=True, exist_ok=True)
    with open(NOTES / "journey.md", "a", encoding="utf-8") as f:
        f.write(text + "\n")
    st["note_lines"] += 1


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def mark(st, tag):
    """记一个本轮事件标签（③ [RULE] 触发 / ④ 痛感钩子共用）。幂等。"""
    evs = st.setdefault("round_events", [])
    if tag not in evs:
        evs.append(tag)


def feel(st, dim, delta):
    """排一个情绪增量（主循环在情感演化后统一结算并夹紧）。"""
    st.setdefault("feelings", []).append([dim, delta])


def push_read(st, kind):
    """记录一次阅读来源（"web"/"arch"/"wider"），滑窗截断（① 配额统计用）。"""
    rr = st.setdefault("recent_reads", [])
    rr.append(kind)
    del rr[:-config.RECENT_READS_MAX]


def web_share(st):
    """最近阅读中 web 缓存占比（①：≥ WEB_SHARE_CAP 时 explore 不再选 web 文件）。"""
    rr = st.get("recent_reads", [])
    if not rr:
        return 0.0
    return sum(1 for k in rr if k == "web") / len(rr)


def record_active(st, concept, cap=80):
    """v9.18 三层 priming：记一个"最近被点亮"的概念（做梦起点必须先被激活——Cai2009）。
    recent_active 存 [{"c": 概念, "round": 轮}]，去重保最新。"""
    if not concept:
        return
    ra = st.setdefault("recent_active", [])
    ra[:] = [x for x in ra if x.get("c") != concept]
    ra.append({"c": concept, "round": st.get("rounds", 0)})
    del ra[:-cap]
