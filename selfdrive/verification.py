# -*- coding: utf-8 -*-
"""教师意见与环境裁决对齐；教师可校准，环境探针优先。"""
import re

from . import claims, conceptcard, config, probes
from .state import append_note, mark


def _teacher_only(st, il, verdict, observation_id, expression, claim_id=None):
    claim = claims.ensure_claim(st, expression, claim_id=claim_id)
    polarity = {"真": "support", "假": "contradict"}.get(verdict, "inconclusive")
    reliability = claims.source_reliability(st, "teacher", "general")
    group = f"teacher:{expression}:{polarity}"
    claims.add_evidence(st, claim, "teacher", observation_id, group,
                        polarity, observation_id, reliability)
    claim["status"] = "hypothesis"
    il["verification_status"] = "teacher_only"
    return claim


def _transform(st, il, verdict, observation_id):
    expr = il.get("premises", [""])[0]
    result = il.get("result") or ""
    expression = f"{expr}→{result}"
    actual = probes.verify_addition(expr, result)
    if actual is None:
        _teacher_only(st, il, verdict, observation_id, expression,
                      claims.stable_id("transform", expression))
        append_note(st, f"- 推理待核：{expr}={result} 超出当前探针范围，教师意见仅作假设证据")
        return

    il["verification_status"] = "environment_verified"
    il["environment_verdict"] = "真" if actual else "假"
    if verdict in ("真", "假"):
        claims.calibrate_source(st, "teacher", "addition", (verdict == "真") == actual)
    claims.record_transform_verdict(st, expr, result, actual, observation_id)
    tkey = expression
    transforms = st.setdefault("transforms", {})
    if actual:
        for existing in list(transforms):
            old_expr, _, old_result = existing.partition("→")
            if old_expr == expr and probes.verify_addition(old_expr, old_result) is False:
                del transforms[existing]
        if tkey not in transforms:
            transforms[tkey] = {
                "operands": re.findall(r"[一二两三四五六七八九十]+", expr),
                "result": result,
                "support": 1,
                "last": st["rounds"],
                "source": "environment_verified",
                "evidence_groups": [observation_id],
            }
        conceptcard.on_transform_verdict(st, True, expr, result, observation_id)
    else:
        transforms = st.setdefault("transforms", {})
        for existing in list(transforms):
            old_expr, _, old_result = existing.partition("→")
            if old_expr == expr and probes.verify_addition(old_expr, old_result) is False:
                del transforms[existing]
        st.setdefault("mistakes", []).append({
            "round": st["rounds"], "premises": [expr], "result": result,
            "kind": "transform_refuted", "source": observation_id,
        })
        del st["mistakes"][:-30]
        mark(st, config.EVT_INFER_FALSE)
        conceptcard.on_transform_verdict(st, False, expr, result, observation_id)
    append_note(st, f"- 双重裁决：教师『{verdict}』，加法探针『{il['environment_verdict']}』；"
                    f"{expr}={result} 以环境结果为准")


def _infer(st, il, verdict, observation_id):
    premises = il.get("premises", [])
    if len(premises) < 2:
        return
    a, c = premises[:2]
    result = il.get("result")
    expression = f"{a}+{c}→{result}"
    _teacher_only(st, il, verdict, observation_id, expression)
    if verdict == "假":
        mistakes = st.setdefault("mistakes", [])
        mistakes.append({"round": st["rounds"], "premises": [a, c],
                         "pattern": sorted([a, c]), "result": result,
                         "source": "teacher_hypothesis"})
        del mistakes[:-30]
        mark(st, config.EVT_INFER_FALSE)
    append_note(st, f"- 推理验证：{a}+{c}→{result or '?'} 教师判定『{verdict}』，仅记为待验证命题，不直接改信念")


def apply_verdict(st, il, verdict, observation_id):
    if il.get("verdict") is not None:
        return
    il["verdict"] = verdict
    il["teacher_verdict"] = verdict
    il["verification_round"] = st["rounds"]
    if il.get("type") == "transform":
        _transform(st, il, verdict, observation_id)
    else:
        _infer(st, il, verdict, observation_id)
