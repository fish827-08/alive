# -*- coding: utf-8 -*-
"""可关闭的 LLM 提案桥：严格 JSON、调用预算、失败降级、无执行权限。"""
import hashlib
import json
import time
import urllib.request

from . import config
from .state import append_jsonl, append_note

_FIELDS = ("proposals", "assumptions", "unknowns", "suggested_checks")


def _budget(st):
    today = time.strftime("%Y%m%d")
    budget = st.setdefault("llm_budget", {})
    if budget.get("day") != today:
        budget.clear()
        budget.update({"day": today, "calls": 0, "fails": 0})
    return budget


def available(st):
    if not config.LLM_ENABLED:
        return False
    if st["rounds"] - st.get("last_llm_round", -999) < config.LLM_COOLDOWN:
        return False
    budget = _budget(st)
    if budget["calls"] >= config.LLM_DAILY_MAX:
        return False
    if budget.get("fails", 0) >= 3:
        if st["rounds"] - budget.get("fail_round", -999) < config.LLM_FAILURE_SILENCE:
            return False
        budget["fails"] = 0
    return True


def _strip_think(text):
    if "<think>" in text and "</think>" not in text:
        return ""
    if "</think>" in text:
        return text.split("</think>", 1)[1].strip()
    return text.strip()


def _failed(st):
    budget = _budget(st)
    budget["fails"] = budget.get("fails", 0) + 1
    budget["fail_round"] = st["rounds"]
    return None


def _parse_result(raw, task, elapsed, prompt):
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or set(parsed) != set(_FIELDS):
        raise ValueError("LLM 输出必须恰好包含四个提案数组")
    result = {"model": config.LLM_MODEL, "task": task, "seconds": round(elapsed, 4)}
    for key in _FIELDS:
        values = parsed[key]
        if not isinstance(values, list) or len(values) > 8:
            raise ValueError(f"{key} 必须是最多 8 项的数组")
        if any(not isinstance(item, str) for item in values):
            raise ValueError(f"{key} 只能包含字符串")
        result[key] = [item.strip()[:config.LLM_PROPOSAL_MAX_CHARS]
                       for item in values if item.strip()]
    result.update({
        "raw": raw,
        "prompt_version": config.LLM_PROMPT_VERSION,
        "input_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    })
    if not any(result[key] for key in _FIELDS):
        raise ValueError("空提案")
    return result


def consult(st, task, prompt, max_chars=900):
    if not available(st):
        return None
    budget = _budget(st)
    budget["calls"] += 1
    st["last_llm_round"] = st["rounds"]
    prompt = prompt[:config.LLM_MAX_PROMPT_CHARS]
    body = json.dumps({
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": config.LLM_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 400},
    }).encode("utf-8")
    started = time.time()
    try:
        req = urllib.request.Request(
            config.OLLAMA_URL.rstrip("/") + "/api/chat", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", errors="strict"))
        content = data.get("message", {}).get("content", "")
        if not isinstance(content, str):
            raise ValueError("LLM content 必须是字符串")
        raw = _strip_think(content)
        result = _parse_result(raw, task, time.time() - started, prompt)
    except Exception:
        return _failed(st)
    budget["fails"] = 0
    budget.pop("fail_round", None)
    result["raw"] = result["raw"][:max_chars]
    return result


def save_proposals(st, result, topic):
    if not result:
        return 0
    props = st.setdefault("llm_proposals", [])
    n = 0
    for field in _FIELDS:
        for content in result.get(field, []):
            if n >= config.LLM_PROPOSALS_PER_CALL:
                break
            record = {
                "id": f"P{st['rounds']:08d}-{len(props) + 1:03d}",
                "kind": field,
                "content": content,
                "topic": topic[:20],
                "task": result.get("task"),
                "scope": "未验证自然语言提案",
                "status": "unverified",
                "round": st["rounds"],
                "verified_round": None,
                "provenance": {
                    "source": "llm",
                    "model": result.get("model"),
                    "prompt_version": result.get("prompt_version"),
                    "input_sha256": result.get("input_sha256"),
                    "raw_sha256": hashlib.sha256(result.get("raw", "").encode("utf-8")).hexdigest(),
                },
            }
            props.append(record)
            append_jsonl("proposals.jsonl", record)
            n += 1
        if n >= config.LLM_PROPOSALS_PER_CALL:
            break
    del props[:max(0, len(props) - 40)]
    if n:
        append_note(st, f"- LLM提案（待验证×{n}，模型 {result.get('model')}）："
                        f"{props[-1]['content'][:80]}——仅入影子提案库，未进入概念或信念")
    return n
