# -*- coding: utf-8 -*-
"""llm_bridge — v9.22 LLM 提案模块（设计稿第 10 节的最小落地）。

权力边界（设计稿第 3 节，必须遵守）：
  LLM 只有提案权——输出是 {proposals, assumptions, unknowns, suggested_checks}
  结构化提案，绝不携带 confidence/approved/执行权限；
  验证权归探针与阅读消化（晋升制），信念权归中枢门禁，行动权归 pick_action。

实现约束（设计稿 10.5）：
  - 通过 Ollama HTTP API 调本地小模型（默认 qwen3:1.7b，用户本机）；
  - 每轮不必调用：技能层有冷却（LETTER 式）+ 每日预算；
  - 超时/schema 校验失败/模型不可用 → 返回 None，无 LLM 路径照常运行；
  - 记录模型名与原始输出摘要（provenance 的雏形）；
  - 云端默认 LLM_ENABLED=False（用户决定：云端不部署 LLM）。
"""
import json
import time
import urllib.request

from . import config


def available(st):
    """LLM 是否可用（开关 + 冷却 + 日预算）。不探活网络——探活放 consult 内部，
    失败自动降级并记一次失败（连续失败进入静默期，避免每轮空耗）。"""
    if not config.LLM_ENABLED:
        return False
    if st["rounds"] - st.get("last_llm_round", -999) < config.LLM_COOLDOWN:
        return False
    # 日预算：按自然日计数
    today = time.strftime("%Y%m%d")
    budget = st.setdefault("llm_budget", {"day": today, "calls": 0, "fails": 0})
    if budget.get("day") != today:
        budget.update({"day": today, "calls": 0, "fails": 0})
    if budget["calls"] >= config.LLM_DAILY_MAX:
        return False
    # 连续失败静默期：连错 3 次，50 轮内不再尝试（Ollama 未启动时不空耗）
    if budget.get("fails", 0) >= 3:
        if st["rounds"] - budget.get("fail_round", -999) < 50:
            return False
        budget["fails"] = 0
    return True


def _strip_think(text):
    """qwen3 系列会输出 <think>…</think> 思考段——剥掉，只留正文。"""
    if "</think>" in text:
        return text.split("</think>", 1)[1].strip()
    return text.strip()


def consult(st, task, prompt, max_chars=900):
    """向本地 LLM 提交一个任务，返回严格结构或 None。
    返回: {"proposals": [...], "assumptions": [...], "unknowns": [...],
           "suggested_checks": [...], "raw": str} 或 None。
    只提案：返回值里没有任何字段能直接改变置信度或触发执行。"""
    if not available(st):
        return None
    body = json.dumps({
        "model": config.LLM_MODEL,
        "messages": [
            {"role": "system", "content": config.LLM_SYSTEM_PROMPT},
            {"role": "user", "content": prompt[:config.LLM_MAX_PROMPT_CHARS]},
        ],
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 400},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            config.OLLAMA_URL.rstrip("/") + "/api/chat", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", errors="ignore"))
        raw = _strip_think(data.get("message", {}).get("content", ""))
    except Exception as exc:
        b = st.setdefault("llm_budget", {"day": time.strftime("%Y%m%d"), "calls": 0, "fails": 0})
        b["fails"] = b.get("fails", 0) + 1
        b["fail_round"] = st["rounds"]
        return None
    st.setdefault("llm_budget", {})["calls"] = \
        st.setdefault("llm_budget", {"calls": 0}).get("calls", 0) + 1
    st["last_llm_round"] = st["rounds"]
    # 解析：优先 JSON 块，失败则整段正文作为单条提案（文本世界的提案本来就是散文）
    proposals = []
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(raw[start:end + 1])
            for key in ("proposals", "assumptions", "unknowns", "suggested_checks"):
                val = parsed.get(key, [])
                if isinstance(val, list):
                    proposals.extend(str(x)[:config.LLM_PROPOSAL_MAX_CHARS] for x in val if x)
            if proposals:
                return {"proposals": proposals, "raw": raw[:max_chars],
                        "model": config.LLM_MODEL, "task": task}
    except Exception:
        pass
    if raw and len(raw) > 20:
        proposals = [raw[:config.LLM_PROPOSAL_MAX_CHARS]]
        return {"proposals": proposals, "raw": raw[:max_chars],
                "model": config.LLM_MODEL, "task": task}
    return None


def save_proposals(st, result, topic):
    """提案落账（设计稿阶段 1 的影子运行）：只记录，不晋升，不进决策。
    Proposal 的验证途径 = 后续自然阅读中同现（晋升制的跨源门槛会自然筛选）
    —— 现在这个函数只做三件事：存 pending、记 provenance、写笔记。"""
    if not result:
        return 0
    props = st.setdefault("llm_proposals", [])
    n = 0
    for p in result.get("proposals", [])[:config.LLM_PROPOSALS_PER_CALL]:
        props.append({
            "content": p, "topic": topic[:20], "task": result.get("task"),
            "model": result.get("model"), "round": st["rounds"],
            "status": "unverified",   # 设计稿 6.2：unverified 是唯一合法初始态
            "verified_round": None,
        })
        n += 1
    del props[:max(0, len(props) - 40)]
    if n:
        from .state import append_note
        append_note(st, f"- LLM提案（待验证×{n}，模型 {result.get('model')}）："
                        f"{props[-1]['content'][:80]}……——这些只是别人的说法，等我读到证据才算数")
    return n
