# -*- coding: utf-8 -*-
"""conceptcard — v9.20 概念卡与定律表：从"字符串+计数器"到"能用的东西"。

设计（docs/概念-数据结构草案.md + 评估修订）：
  概念卡七面：接地(探针+样例) / 时机(affordance) / 组合(frame) / 关系(links) /
              定律引用(laws) / 履历(history, append-only) / 状态(典型度/激活)
  Laws 独立表：定律不归概念所有，概念只引用——置信度更新不打架。
  过程环：处境→affordance 匹配激活→用定律预测→探针验证→命中(置信↑/样例+)
          /反驳(记黑/降信)→巩固期样例压缩成典型度、零反驳的假设毕业为信念。
  记忆双层：history=情景记忆(按典型度加权遗忘)，laws/concepts=语义记忆(慢变)。

探针五类（辉月的文本世界里可执行的"核对世界"只有这五种，全部已存在）：
  COUNT 数共现 ｜ SEARCH 查语料/kb ｜ ASK 问老师 ｜ COMPOSE 变换组合推演 ｜ ABLATE 虚拟屏蔽

v9.20 先点亮两张卡：『加法』(探针=COMPOSE) 和『能量』(读自身 state 算缺口)。
验收（草案第 8 节）：没人叫它算时，它会不会主动算"我还差多少"。
"""
import random

from . import claims, config, probes, teacherless
from .state import append_note

HISTORY_KEEP = 12            # 每卡履历保留条数（情景记忆遗忘：旧的淡出）
EXEMPLARS_GRADUATE = 5       # 样例数毕业门槛：hypothesis → belief
CONFIDENCE_GRADUATE = 0.75
HISTORY_FADE = 400           # 轮龄超过此值的低权履历在巩固期淡出

# 五类文本探针，加一个读取自身内稳态的内部探针
PROBES = ("COUNT", "SEARCH", "ASK", "COMPOSE", "ABLATE", "READ_STATE")


def default_card(name):
    return {"ground": {"probes": [], "exemplars": []},
            "affordances": [], "frame": {}, "links": {},
            "laws": [], "history": [],
            "stats": {"typicality": 0.5, "activation": 0.0, "base_rate": 0}}


def ensure_cards(st):
    """点亮初始卡片（幂等）。加法卡从 transforms 已验证条目取种子样例。"""
    cards = st.setdefault("concept_cards", {})
    if "加法" not in cards:
        c = default_card("加法")
        c["ground"]["probes"] = ["COMPOSE"]
        c["frame"] = {"slots": ["A", "B", "→R"], "fills_from": "transforms"}
        c["affordances"] = ["遇到两个量要合并", "变换组合/复检通道触发"]
        c["links"] = {"inverse": ["减法"], "part_of": ["算术"]}
        # 种子样例：support 最高的已验证变换
        tfs = st.get("transforms", {})
        verified = [(k, v) for k, v in tfs.items()
                    if probes.verify_addition(*k.partition("→")[::2]) is True]
        for tkey, tf in sorted(verified, key=lambda kv: -kv[1].get("support", 0))[:3]:
            expr, _, res = tkey.partition("→")
            c["ground"]["exemplars"].append((expr, res, "历史验证"))
        cards["加法"] = c
    if "能量" not in cards:
        c = default_card("能量")
        c["ground"]["probes"] = ["READ_STATE"]
        c["frame"] = {"slots": ["当前", "消耗", "→缺口"], "fills_from": "state.energy"}
        c["affordances"] = ["energy < 0.35", "要选耗能动作之前"]
        cards["能量"] = c
    # 种子定律：合并计数 = 计数相加。代码先验，不是实验证据：
    # support=0、confidence 仅作声明初值，等变换验证来喂证据。
    laws = st.setdefault("laws", {})
    if "L001" not in laws:
        laws["L001"] = {"form": "count(merge(A,B)) = add(count(A),count(B))",
                        "concepts": ["加法"], "confidence": 0.4, "support": 0,
                        "confidence_basis": "declared_prior_uncalibrated",
                        "falsifiers": [], "status": "hypothesis",
                        "source": "base_code_prior"}
        cards["加法"]["laws"].append("L001")
    return cards


# ---------- 处境匹配（affordance → 激活 → 卡乘子） ----------

def situation_match(st):
    """每轮在 pick_action 之前跑：哪些卡的 affordance 被当前处境点亮。
    返回 {卡名: 原因}；同时这是"使用即学习"的入口——点亮本身就记履历+涨激活。
    能量卡的用法（草案验收项）：低能量时主动算缺口，用缺口决定 rest/explore 倾向。"""
    ensure_cards(st)
    hits = {}
    en = st.get("energy", 1.0)
    if en < 0.35:
        gap = round(en - 0.35, 3)          # 负数=缺口
        hits["能量"] = f"energy={en:.2f} < 0.35，缺口 {abs(gap):.2f}"
        st["card_boost"] = {"rest": 1.6, "explore_file": 0.85, "web_search": 0.85}
        append_note(st, f"- 概念使用（能量卡）：能量 {en:.2f} 低于阈值，算得缺口 {abs(gap):.2f}"
                        f"——按『能量』卡的决定倾向：先休息回能，缓一缓耗能动作")
    if not teacherless.is_enabled():
        unread = [p for p in config.OUTBOX.glob("*.md") if p.stem not in st.get("brain_read", [])] \
            if config.OUTBOX.exists() else []
        if unread:
            hits.setdefault("回答", f"未读回答 {len(unread)} 条")
    for name, why in hits.items():
        use_card(st, name, "activated", why)
    return hits


def use_card(st, name, event, detail=""):
    """使用即学习：每次激活/验证/反驳都记履历、涨激活与基础率。"""
    cards = st.get("concept_cards", {})
    if name not in cards:
        cards[name] = default_card(name)
    c = cards[name]
    c["history"].append({"round": st["rounds"], "event": event, "detail": detail[:40]})
    del c["history"][:max(0, len(c["history"]) - HISTORY_KEEP * 2)]   # 巩固期再压到 KEEP
    s = c["stats"]
    s["activation"] = min(1.0, s.get("activation", 0.0) + 0.2)
    s["base_rate"] = s.get("base_rate", 0) + 1


# ---------- 探针 ----------

def probe_COMPOSE(st, a=None, b=None):
    """加法探针：不内置算术——用已验证变换链归约出 a加b（learning.transform_compose 的定向版）。"""
    from .learning import transform_compose
    if a is None or b is None:
        tc = transform_compose(st)
        return tc  # (expr, result, via) 或 None
    # 定向：查直接表
    tfs = st.get("transforms", {})
    direct = {}
    for tkey, v in tfs.items():
        expr, _, result = tkey.partition("→")
        if probes.verify_addition(expr, result) is not True:
            continue
        ops = tuple(sorted(v.get("operands", [])))
        if ops and v.get("result"):
            direct[ops] = v["result"]
    key = tuple(sorted([a, b]))
    if key in direct:
        return (f"{a}加{b}", direct[key], f"直接查表：{a}加{b}={direct[key]}")
    return None


def probe_ENERGY(st):
    """能量探针：读自身 state，算缺口——概念第一次被用来做决定的地方。"""
    en = st.get("energy", 1.0)
    return {"energy": round(en, 3), "gap_to_threshold": round(en - 0.35, 3),
            "advice": "先休息" if en < 0.35 else "可以行动"}


# ---------- 定律表（验证回写） ----------

def on_transform_verdict(st, ok, expr="", result="", observation_id=""):
    """正确算式支持合并计数定律；错误候选只反驳该算式，不冒充定律反例。"""
    laws = st.get("laws", {})
    card = st.get("concept_cards", {}).get("加法")
    law = laws.get("L001")
    if not law or not card:
        return
    law.setdefault("id", "L001")
    law.setdefault("supporting_evidence", [])
    law.setdefault("counter_evidence", [])
    law.setdefault("evidence_index", {})
    law.setdefault("independent_support", 0)
    law.setdefault("independent_counter", 0)
    if not ok:
        use_card(st, "加法", "candidate_refuted", "错误候选被探针排除")
        return
    group = f"transform:{expr}→{result}"
    _, is_new = claims.add_evidence(
        st, law, "arithmetic_probe", observation_id, group, "support",
        observation_id or group, reliability=1.0)
    if is_new:
        law["confidence"] = min(0.95, law.get("confidence", 0.4) + 0.08)
        use_card(st, "加法", "verified", "变换组合经探针判真")
        s = card["stats"]
        s["typicality"] = min(1.0, s.get("typicality", 0.5) + 0.05)
    graduate(st)


def graduate(st):
    """晋升仪式：样例足、零反驳、置信达标的 hypothesis 毕业 belief；belief 被反驳降回重审。"""
    for lid, law in st.get("laws", {}).items():
        if law["status"] == "hypothesis" and (
                law.get("support", 0) >= EXEMPLARS_GRADUATE
                and law.get("independent_support", 0) >= EXEMPLARS_GRADUATE
                and not law.get("falsifiers")
                and law.get("confidence", 0) >= CONFIDENCE_GRADUATE):
            law["status"] = "belief"
            append_note(st, f"- 定律毕业：{lid} 『{law['form'][:40]}』从假设升为信念"
                            f"（support={law['support']}，零反驳，置信 {law['confidence']:.2f}）")
        elif law["status"] == "belief" and law.get("falsifiers"):
            law["status"] = "hypothesis"
            law["confidence"] = max(0.3, law["confidence"] - 0.2)
            append_note(st, f"- 定律重审：{lid} 被反驳，降回假设重审")


# ---------- 巩固（情景 → 语义压缩） ----------

def consolidate_cards(st):
    """巩固期：履历按轮龄淡出（情景记忆遗忘）；样例压缩进典型度；激活衰减。"""
    faded = 0
    for c in st.get("concept_cards", {}).values():
        h = c["history"]
        keep = [x for x in h if st["rounds"] - x.get("round", 0) < HISTORY_FADE]
        faded += len(h) - len(keep)
        recent = keep[-HISTORY_KEEP:]
        # 典型度 = 近期验证占比与样例数的联合（粗糙版：验证越多越典型）
        verified = sum(1 for x in recent if x.get("event") == "verified")
        if recent:
            c["stats"]["typicality"] = round(
                0.5 * c["stats"].get("typicality", 0.5)
                + 0.5 * min(1.0, verified / max(1, len(recent)) + len(c["ground"]["exemplars"]) / 10), 3)
        c["stats"]["activation"] = round(max(0.0, c["stats"].get("activation", 0) - 0.1), 3)
        c["history"] = recent
    return faded
