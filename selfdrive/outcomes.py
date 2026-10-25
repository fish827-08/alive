# -*- coding: utf-8 -*-
"""行动前预测、行动后结算、校准与信用归因。"""
import time

from .state import append_jsonl

OUTCOME_KEEP = 200


def _recover_interrupted(st):
    previous = st.pop("current_action", None)
    if not previous:
        return
    record = dict(previous)
    record.update({
        "status": "inconclusive",
        "actual": {"observation": "interrupted", "state_changes": {}, "detail": "进程在结算前中断"},
        "brier": None,
        "settled_at": time.time(),
    })
    append_jsonl("action_outcomes.jsonl", record)


def begin(st, action, candidates):
    _recover_interrupted(st)
    candidate = next((c for c in candidates if c.get("action") == action), {})
    wm = st.get("working_memory", {})
    current = {
        "id": f"A{st['rounds']:08d}-{action}",
        "round": st["rounds"],
        "action": action,
        "status": "pending",
        "context": {
            "goal": wm.get("goal"),
            "energy": st.get("energy", 1.0),
            "emotions": dict(st.get("emotions", {})),
        },
        "expected": {
            "observation": "action_success" if candidate.get("success_probability", 0.5) >= 0.5 else "action_failure",
            "probability": candidate.get("success_probability", 0.5),
            "energy_delta": candidate.get("expected_energy_delta", -0.008),
            "horizon": 1,
        },
        "started_at": time.time(),
        "before": {
            "concepts": len(st.get("concepts", {})),
            "relations": len(st.get("relations", {})),
            "transforms": len(st.get("transforms", {})),
        },
    }
    st["current_action"] = current
    append_jsonl("action_outcomes.jsonl", current)
    return current


def _attribution(current, ok, actual_energy_delta):
    expected = current["expected"]
    probability = expected["probability"]
    reasons = []
    if not ok:
        reasons.append("skill_or_precondition" if probability >= 0.5 else "expected_failure")
    elif probability < 0.5:
        reasons.append("positive_surprise")
    else:
        reasons.append("expected_success")
    if abs(actual_energy_delta - expected["energy_delta"]) > 0.0001:
        reasons.append("energy_model_mismatch")
    return reasons


def settle(st, ok, detail, forced_status=None):
    current = st.pop("current_action", None)
    if not current:
        return None
    energy = st.get("energy", 1.0)
    before_energy = current["context"]["energy"]
    actual_delta = round(energy - before_energy, 6)
    after = {
        "concepts": len(st.get("concepts", {})),
        "relations": len(st.get("relations", {})),
        "transforms": len(st.get("transforms", {})),
    }
    state_changes = {
        "energy_delta": actual_delta,
        "emotion_delta": {
            key: round(st.get("emotions", {}).get(key, 0.0) - value, 6)
            for key, value in current["context"]["emotions"].items()
        },
        "new_concepts": after["concepts"] - current["before"]["concepts"],
        "new_relations": after["relations"] - current["before"]["relations"],
        "new_transforms": after["transforms"] - current["before"]["transforms"],
    }
    expected_success = current["expected"]["observation"] == "action_success"
    status = forced_status or ("hit" if bool(ok) == expected_success else "miss")
    probability = current["expected"]["probability"]
    brier = None if status == "inconclusive" else round((probability - float(bool(ok))) ** 2, 6)
    outcome = dict(current)
    outcome.update({
        "status": status,
        "actual": {
            "observation": "action_success" if ok else "action_failure",
            "state_changes": state_changes,
            "detail": detail[:300],
        },
        "cost": {
            "energy": round(-actual_delta, 6),
            "seconds": round(max(0.0, time.time() - current["started_at"]), 4),
        },
        "errors": [] if ok else [detail[:160]],
        "side_effects": [],
        "attribution": _attribution(current, ok, actual_delta),
        "brier": brier,
        "settled_at": time.time(),
    })
    st.setdefault("action_outcomes", []).append(outcome)
    del st["action_outcomes"][:-OUTCOME_KEEP]
    if brier is not None:
        stats = st.setdefault("outcome_stats", {}).setdefault(
            current["action"], {"n": 0, "brier_sum": 0.0, "hit": 0})
        stats["n"] += 1
        stats["brier_sum"] = stats.get("brier_sum", 0.0) + brier
        stats["brier"] = round(stats["brier_sum"] / stats["n"], 5)
        stats["hit"] = stats.get("hit", 0) + int(status == "hit")
        stats["last_round"] = st["rounds"]
    append_jsonl("action_outcomes.jsonl", outcome)
    return outcome


def metrics(st):
    outcomes = st.get("action_outcomes", [])
    briers = [o["brier"] for o in outcomes if isinstance(o.get("brier"), (int, float))]
    return {
        "n": len(outcomes),
        "brier": round(sum(briers) / len(briers), 4) if briers else None,
        "hit": sum(o.get("status") == "hit" for o in outcomes),
    }
