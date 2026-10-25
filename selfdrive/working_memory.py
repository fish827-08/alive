# -*- coding: utf-8 -*-
"""容量受限的当前工作记忆；构建过程只读长期记忆，不强化边权。"""
from . import config, research, teacherless


def build(st):
    energy = st.get("energy", 1.0)
    unread = []
    if not teacherless.is_enabled() and config.OUTBOX.exists():
        unread = [p.stem for p in config.OUTBOX.glob("*.md")
                  if p.stem not in st.get("brain_read", [])]
    if energy < 0.35:
        goal = "restore_energy"
    elif unread:
        goal = "read_teacher_response"
    else:
        goal = "learn_and_verify"

    active = []
    for item in reversed(st.get("recent_active", [])):
        concept = item.get("c")
        if concept and concept not in active:
            active.append(concept)
        if len(active) >= 12:
            break
    topic = (st.get("last_kw") or "").split("、")[0]
    if topic and topic not in active:
        active.insert(0, topic)
        del active[12:]

    active_set = set(active)
    claims = []
    for claim in st.get("claims", {}).values():
        expression = claim.get("expression", "")
        if claim.get("last_tested") is not None and any(
                concept in expression for concept in active_set):
            claims.append(claim["id"])
    pending = [
        {"a": p.get("a"), "b": p.get("b"), "deadline": p.get("deadline")}
        for p in st.get("edge_preds", []) if p.get("status") == "pending"
    ][:5]
    contradictions = [
        claim["id"] for claim in st.get("claims", {}).values()
        if claim.get("status") == "refuted"
    ][-5:]
    wm = {
        "round": st.get("rounds", 0),
        "goal": goal,
        "homeostasis": {
            "energy": energy,
            "energy_gap_to_threshold": round(energy - 0.35, 4),
            "unread_responses": len(unread),
        },
        "active_concepts": active,
        "active_claims": claims[:8],
        "research_focus": research.focus(st, limit=3),
        "open_predictions": pending,
        "contradictions": contradictions,
        "available_skills": [s for s in config.SKILLS if teacherless.allow_action(s)],
        "candidates": [],
        "selected_action": None,
    }
    if teacherless.is_enabled():
        wm["mode"] = "teacherless"
    st["working_memory"] = wm
    return wm


def attach_decision(st, action, candidates):
    wm = st.setdefault("working_memory", {})
    wm["candidates"] = candidates
    wm["selected_action"] = action
    wm["decision_mode"] = config.DECISION_MODE
    return wm
