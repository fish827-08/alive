# -*- coding: utf-8 -*-
"""rules_compiler — [RULE] 陈述→动作 编译器（v9.16 ③）。

病根（v9.11→v9.15 复发三次）：老师的教导被"正确地读、错误地降维"——
进笔记/模板/停用词表，永远进不了 pick_action。本模块给陈述句一条编译路径：
大脑回答里的 [RULE] 块直接变成决策层的一部分。

DSL（详见 brain/PROTOCOL.md）：
  [RULE] <触发> -> <效果>[ & <效果>…] [/RULE]
  触发（轮事件标签）：任何 / 提问 / 重复提问 / 读回答 / 读自缓存 /
                      预测命中 / 预测错误 / 推理判假 / 搜索
  效果：
    权重:<技能>×<系数>     持久乘子进 pick_action（夹紧 0.05~3.0）
    情绪:<维度><+|-><数值> 触发当轮生效
    合并:<碎片>→<全词>     概念合并（计数+边权转移，替代旧"拉黑"）
    模板:<文本>            新增提问模板
    撤销:<rid>             删除指定规则并回滚其乘子

安全边界：规则 ≤30 条（LRU）；乘子夹紧；每条规则带出生快照（重复率基线），
之后可对照——"听懂"的验收标准就是指标真的变了。
"""
import re

from . import config
from .config import (EVT_ANY, MULTIPLIER_MAX, MULTIPLIER_MIN, RULE_MAX)
from .state import append_note, feel, log_event

RULE_RE = re.compile(r"\[RULE\]\s*(.+?)\s*\[/RULE\]", re.S)
_ARROW = re.compile(r"\s*(?:->|→|=>)\s*")

# 触发词归一：老师可用自然写法
_TRIGGER_ALIASES = {
    "任何": config.EVT_ANY, "总是": config.EVT_ANY, "每次": config.EVT_ANY,
    "提问": config.EVT_ASK, "问问题时": config.EVT_ASK,
    "重复提问": config.EVT_DUP_ASK, "重复发问": config.EVT_DUP_ASK, "又重复": config.EVT_DUP_ASK,
    "读回答": config.EVT_READ_ANSWER, "读到大脑回答": config.EVT_READ_ANSWER,
    "读自缓存": config.EVT_READ_CACHE, "读自己缓存": config.EVT_READ_CACHE,
    "吃自己缓存": config.EVT_READ_CACHE,
    "预测命中": config.EVT_PRED_HIT, "猜对": config.EVT_PRED_HIT,
    "预测错误": config.EVT_PRED_MISS, "预测失误": config.EVT_PRED_MISS, "猜错": config.EVT_PRED_MISS,
    "推理判假": config.EVT_INFER_FALSE, "推理被判假": config.EVT_INFER_FALSE,
    "搜索": config.EVT_SEARCH, "上网搜索": config.EVT_SEARCH,
}

_EFF_WEIGHT = re.compile(r"^权重[:：]\s*([a-z_]+)\s*[×x\*]\s*([0-9.]+)$")
_EFF_EMOTION = re.compile(r"^情绪[:：]\s*([a-zA-Z\u4e00-\u9fa5_]+)\s*([+\-−])\s*([0-9.]+)$")
_EFF_MERGE = re.compile(r"^合并[:：]\s*(.+?)\s*(?:->|→|=>)\s*(.+)$")
_EFF_TEMPLATE = re.compile(r"^模板[:：]\s*(.+)$")
_EFF_UNDO = re.compile(r"^撤销[:：]\s*(r\d+)$")


def _parse_effect(s):
    s = s.strip()
    m = _EFF_WEIGHT.match(s)
    if m and m.group(1) in config.SKILLS:
        return {"kind": "weight", "skill": m.group(1), "factor": float(m.group(2))}
    m = _EFF_EMOTION.match(s)
    if m:
        val = float(m.group(3))
        return {"kind": "emotion", "dim": m.group(1),
                "delta": -val if m.group(2) in ("-", "−") else val}
    m = _EFF_MERGE.match(s)
    if m:
        return {"kind": "merge", "drop": m.group(1).strip(), "keep": m.group(2).strip()}
    m = _EFF_TEMPLATE.match(s)
    if m and 0 < len(m.group(1)) <= 200:
        return {"kind": "template", "text": m.group(1).strip()}
    m = _EFF_UNDO.match(s)
    if m:
        return {"kind": "undo", "rid": m.group(1)}
    return None


def parse_and_compile(st, text, source):
    """从大脑回答文本编译 [RULE] 块。返回编译条数。"""
    n = 0
    for block in RULE_RE.findall(text):
        parts = _ARROW.split(block.strip(), maxsplit=1)
        if len(parts) != 2:
            continue
        trig_raw, effs_raw = parts
        trigger = _TRIGGER_ALIASES.get(trig_raw.strip())
        if trigger is None:
            continue
        effects = []
        for piece in re.split(r"\s*[&＆;；]\s*", effs_raw):
            eff = _parse_effect(piece)
            if eff:
                effects.append(eff)
        if not effects:
            continue
        # 撤销是特殊指令：立即执行，不占规则位
        if len(effects) == 1 and effects[0]["kind"] == "undo":
            _undo_rule(st, effects[0]["rid"], source)
            n += 1
            continue
        rules = st.setdefault("compiled_rules", {})
        rid = f"r{len(rules) + 1:03d}"
        while rid in rules:
            rid = "r" + str(int(rid[1:]) + 1).zfill(3)
        if len(rules) >= RULE_MAX:   # LRU：淘汰最旧
            oldest = min(rules, key=lambda k: rules[k].get("born_round", 0))
            _undo_rule(st, oldest, source)
        rule = {"trigger": trigger, "effects": effects, "born_round": st["rounds"],
                "source": source, "fires": 0}
        rules[rid] = rule
        # 出生快照（听懂测试基线）
        from .templates import dup_rate
        st.setdefault("rule_metrics", {})[rid] = {
            "born_round": st["rounds"], "dup_rate": round(dup_rate(st), 3)}
        # 乘子类效果立即持久化进决策层
        for eff in effects:
            if eff["kind"] == "weight":
                mult = st.setdefault("skill_multipliers", {})
                base = mult.get(eff["skill"], 1.0)
                mult[eff["skill"]] = max(MULTIPLIER_MIN, min(MULTIPLIER_MAX, base * eff["factor"]))
        desc = " & ".join(_eff_text(e) for e in effects)
        append_note(st, f"- 规则编译：[{rid}] 当「{trigger}」→ {desc}（来自 {source}）——这条陈述已进入我的决策层")
        log_event(st, "rule_compile", f"[{rid}] {trigger} -> {desc}（{source}）")
        n += 1
    return n


def _eff_text(eff):
    k = eff["kind"]
    if k == "weight":
        return f"权重:{eff['skill']}×{eff['factor']}"
    if k == "emotion":
        return f"情绪:{eff['dim']}{'+' if eff['delta'] >= 0 else ''}{eff['delta']}"
    if k == "merge":
        return f"合并:{eff['drop']}→{eff['keep']}"
    if k == "template":
        return f"模板:{eff['text'][:40]}"
    return k


def _undo_rule(st, rid, source=""):
    rules = st.get("compiled_rules", {})
    rule = rules.pop(rid, None)
    if not rule:
        return
    # 回滚它设置的乘子（按出生值近似：乘子重算为 1.0 基线的逆操作不可靠，直接复位该技能）
    for eff in rule.get("effects", []):
        if eff["kind"] == "weight":
            st.setdefault("skill_multipliers", {}).pop(eff["skill"], None)
    st.get("rule_metrics", {}).pop(rid, None)
    append_note(st, f"- 规则撤销：[{rid}]（{source or '老师指令'}）已从决策层移除")


def apply_round_rules(st):
    """轮末结算：本轮事件触发编译规则的一次性效果（情绪/合并/模板）。
    乘子已在编译时持久化，由 decision 层每轮应用。返回触发条数。"""
    events = set(st.get("round_events", []))
    fired = 0
    for rid, rule in st.get("compiled_rules", {}).items():
        trig = rule.get("trigger")
        if trig != EVT_ANY and trig not in events:   # "任何"=无条件触发
            continue
        fired += 1
        rule["fires"] = rule.get("fires", 0) + 1
        for eff in rule.get("effects", []):
            k = eff["kind"]
            if k == "emotion":
                feel(st, eff["dim"], eff["delta"])
            elif k == "merge" and not rule.get("merge_done"):
                from .learning import merge_concepts
                if merge_concepts(st, eff["drop"], eff["keep"]):
                    rule["merge_done"] = True
                    append_note(st, f"- 规则生效 [{rid}]：概念『{eff['drop']}』并入『{eff['keep']}』（老师纠错 → 结构变化）")
                else:
                    rule["merge_done"] = True   # 无可合并也标记，避免每轮重试
            elif k == "template" and not rule.get("tpl_done"):
                from .templates import save_template
                added, tpl_id = save_template(eff["text"])
                if added:
                    append_note(st, f"- 规则生效 [{rid}]：新模板 [{tpl_id}] {eff['text'][:40]}")
                rule["tpl_done"] = True
    return fired


def audit_rules(st):
    """听懂测试：对照各规则出生快照与当前指标（诊断/回望用）。"""
    from .templates import dup_rate
    out = []
    for rid, m in st.get("rule_metrics", {}).items():
        rule = st.get("compiled_rules", {}).get(rid)
        if not rule:
            continue
        out.append(f"[{rid}] 触发{rule.get('fires',0)}次 | 重复率 出生{m.get('dup_rate',0):.0%} → 现在{dup_rate(st):.0%}")
    return out
