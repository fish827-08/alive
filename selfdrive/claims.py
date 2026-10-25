# -*- coding: utf-8 -*-
"""提案、命题、证据与来源校准。重复同源证据不增加独立支持。"""
import hashlib

from .state import append_jsonl

EVIDENCE_KEEP = 200


def stable_id(prefix, expression):
    digest = hashlib.sha256(expression.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def ensure_claim(st, expression, claim_id=None, scope="未限定", falsifier="出现相反证据"):
    claims = st.setdefault("claims", {})
    cid = claim_id or stable_id("claim", expression)
    claim = claims.get(cid)
    if claim is None:
        claim = {
            "id": cid,
            "expression": expression,
            "scope": scope,
            "status": "hypothesis",
            "confidence": 0.5,
            "support": 0,
            "independent_support": 0,
            "independent_counter": 0,
            "supporting_evidence": [],
            "counter_evidence": [],
            "falsifier": falsifier,
            "evidence_index": {},
            "created_round": st.get("rounds", 0),
            "last_tested": None,
        }
        claims[cid] = claim
    return claim


def add_evidence(st, claim, source, source_instance, independence_group,
                 polarity, observation_id, reliability=0.5):
    if polarity not in ("support", "contradict", "inconclusive"):
        raise ValueError(f"unknown evidence polarity: {polarity}")
    index = claim.setdefault("evidence_index", {})
    existing_id = index.get(independence_group)
    if existing_id:
        for evidence in reversed(st.get("evidence", [])):
            if evidence.get("id") == existing_id:
                evidence["repeat_count"] = evidence.get("repeat_count", 1) + 1
                evidence["last_seen_round"] = st.get("rounds", 0)
                break
        return existing_id, False

    sequence = st.get("evidence_seq", 0) + 1
    st["evidence_seq"] = sequence
    eid = f"E{sequence:06d}"
    evidence = {
        "id": eid,
        "claim_id": claim["id"],
        "observation_id": observation_id,
        "source": source,
        "source_instance": source_instance,
        "independence_group": independence_group,
        "reliability": round(max(0.0, min(1.0, reliability)), 4),
        "polarity": polarity,
        "round": st.get("rounds", 0),
        "repeat_count": 1,
    }
    st.setdefault("evidence", []).append(evidence)
    del st["evidence"][:-EVIDENCE_KEEP]
    index[independence_group] = eid
    target = "supporting_evidence" if polarity == "support" else "counter_evidence"
    if polarity != "inconclusive":
        claim.setdefault(target, []).append(eid)
    if polarity == "support":
        claim["independent_support"] = claim.get("independent_support", 0) + 1
        claim["support"] = claim.get("support", 0) + 1
    elif polarity == "contradict":
        claim["independent_counter"] = claim.get("independent_counter", 0) + 1
    claim["last_tested"] = st.get("rounds", 0)
    append_jsonl("evidence.jsonl", evidence)
    return eid, True


def calibrate_source(st, source, domain, agrees):
    sources = st.setdefault("source_reliability", {})
    entry = sources.setdefault(source, {})
    stats = entry.setdefault(domain, {"agree": 0, "disagree": 0})
    key = "agree" if agrees else "disagree"
    stats[key] += 1
    total = stats["agree"] + stats["disagree"]
    stats["reliability"] = round((stats["agree"] + 1) / (total + 2), 4)
    stats["last_round"] = st.get("rounds", 0)
    return stats["reliability"]


def source_reliability(st, source, domain):
    return st.get("source_reliability", {}).get(source, {}).get(domain, {}).get("reliability", 0.5)


def record_transform_verdict(st, expr, result, ok, observation_id, source="arithmetic_probe"):
    expression = f"{expr}→{result}"
    claim = ensure_claim(
        st, expression, claim_id=stable_id("transform", expression),
        scope="封闭中文数词加法世界", falsifier="加法探针得到相反结果")
    group = f"probe:{expression}"
    add_evidence(
        st, claim, source, observation_id, group,
        "support" if ok else "contradict", observation_id, reliability=1.0)
    claim["status"] = "believed" if ok else "refuted"
    claim["confidence"] = 0.99 if ok else 0.01
    return claim
