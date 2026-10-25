# -*- coding: utf-8 -*-
"""research — 阶段 2：研究任务队列 + 定向搜索规划。

边界（与 claims/预测/验证器分开）：
  - 本模块只回答三件事：缺什么信息、为什么值得查、下一次查什么。
  - 搜索结果只是候选材料：登记 URL、获取时间、摘要原文与哈希，状态 unverified；
    不写 claims.evidence、不晋升定律、不把“搜到了”当成“验证了”。
  - 不访问教师/LLM/兄弟信箱；只读本地状态与搜索配置。

任务来源（不依赖 LLM）：
  prediction_miss     可失败预测落空 → 缺中间概念/条件/反例
  claim_evidence_gap  命题独立支持与独立反证都为 0 → 证据缺口
  relation_unverified 仅由同现建立的弱边（w<=2, n<=1）→ 关系缺独立验证
  isolated_concept    已晋升但没有任何关系边的概念 → 无法参与推理
  exploration         队列无可用任务时的有界回退（每 200 轮一段）
"""
import hashlib
import json
import re
import time
import urllib.parse
from collections import Counter

from . import config, tokenizer
from .state import append_jsonl

FORMAT = "alive-research-state/1"
SOURCE_FORMAT = "alive-research-source/2"
RESEARCH_SCHEMA_VERSION = 3   # 3：迁移升级语义 + 计划查询作废 + 来源校验 + 待验证归档

KIND_BASE = {
    "prediction_miss": 0.80,
    "claim_evidence_gap": 0.70,
    "relation_unverified": 0.60,
    "isolated_concept": 0.55,
    "exploration": 0.40,
}
RELATION_KINDS = {"prediction_miss", "relation_unverified"}
KIND_LABELS = {
    "prediction_miss": "预测落空",
    "claim_evidence_gap": "命题证据缺口",
    "relation_unverified": "关系未验证",
    "isolated_concept": "孤立概念",
    "exploration": "探索性检索",
}
KIND_PURPOSES = {
    "prediction_miss": {"bridge", "mechanism", "relation"},
    "claim_evidence_gap": {"evidence", "counterexample", "definition"},
    "relation_unverified": {"relation", "evidence", "counterexample"},
    "isolated_concept": {"definition", "context", "mechanism"},
    "exploration": {"definition", "context", "mechanism"},
}
PURPOSE_LABELS = {
    "evidence": "找支持/反驳证据",
    "counterexample": "找反例与不成立条件",
    "definition": "查定义与边界",
    "mechanism": "查机制与原因",
    "relation": "查两者关系",
    "bridge": "找中间概念/桥接条件",
    "context": "查相关概念与用例",
    "broadened": "失败后放宽角度",
}
CLOSED = {"exhausted", "blocked", "abandoned"}
HISTORY_KEEP = 5   # attempt_history / kind_history 有界保留；迁移截断会写日志
EXECUTED_QUERY_STATUSES = {"used", "success", "failed"}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _canonical_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stable_id(prefix, key):
    return f"{prefix}:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"


def _clean_topic(text, max_len=48):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.strip("#-*`>\"'“”‘’")
    return text[:max_len].strip()


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _has_materials(task):
    if not isinstance(task, dict):
        return False
    return (_int(task.get("result_count"), 0) > 0
            or bool(task.get("source_ids"))
            or task.get("verification") == "pending_verification")


def _bounded_history(entries):
    """按轮次排序后只保留最近 HISTORY_KEEP 条；返回 (保留, 截断数)。"""
    rows = [dict(x) for x in (entries or []) if isinstance(x, dict)]
    rows.sort(key=lambda x: _int(x.get("round"), 0))
    dropped = max(0, len(rows) - HISTORY_KEEP)
    return (rows[-HISTORY_KEEP:] if rows else []), dropped


def is_valid_source_url(url):
    """来源必须是绝对 HTTP(S) URL；空串、相对链接与其它协议一律拒绝。"""
    text = str(url or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def ensure_state(st):
    r = st.setdefault("research", {})
    if not isinstance(r, dict):
        r = {}
        st["research"] = r
    for key in ("tasks", "queries"):
        if not isinstance(r.get(key), dict):
            r[key] = {}
    for key in ("recent_queries", "sources", "config_history", "archived_tasks"):
        if not isinstance(r.get(key), list):
            r[key] = []
    if not isinstance(r.get("miss_backlog"), dict):
        r["miss_backlog"] = {}
    if r.get("config_sha256") is not None and not isinstance(r.get("config_sha256"), str):
        r["config_sha256"] = None
    stats = r.setdefault("stats", {})
    if not isinstance(stats, dict):
        stats = {}
        r["stats"] = stats
    for key in ("tasks_created", "queries_planned", "queries_executed",
                "query_failures", "duplicates_blocked", "sources_recorded",
                "rewrites_planned", "tasks_archived", "source_candidates_rejected"):
        stats.setdefault(key, 0)
    if _int(r.get("schema_version"), 1) < RESEARCH_SCHEMA_VERSION:
        _migrate_legacy_state(st, r)
        r["schema_version"] = RESEARCH_SCHEMA_VERSION
    return r


def _log(st, event, **fields):
    row = {"ts": _now(), "round": st.get("rounds", 0), "event": event}
    row.update(fields)
    append_jsonl("research.jsonl", row)


def effective_config():
    """影响研究调度/搜索策略的参数快照；ask_config 的 research 段一并记录。"""
    ask = config.load_ask_config()
    if not isinstance(ask, dict):
        ask = {}
    ws = ask.get("web_search") if isinstance(ask.get("web_search"), dict) else {}
    research_ask = ask.get("research") if isinstance(ask.get("research"), dict) else {}
    return {
        "version": config.RESEARCH_CONFIG_VERSION,
        "max_tasks": config.RESEARCH_MAX_TASKS,
        "task_keep": config.RESEARCH_TASK_KEEP,
        "new_per_round": config.RESEARCH_NEW_PER_ROUND,
        "queries_per_task": config.RESEARCH_QUERIES_PER_TASK,
        "attempts_per_task": config.RESEARCH_ATTEMPTS_PER_TASK,
        "max_rewrites": config.RESEARCH_MAX_REWRITES,
        "query_history": config.RESEARCH_QUERY_HISTORY,
        "dup_jaccard": config.RESEARCH_DUP_JACCARD,
        "scan_limit": config.RESEARCH_SCAN_LIMIT,
        "miss_backlog": config.RESEARCH_MISS_BACKLOG,
        "archive_keep": config.RESEARCH_ARCHIVE_KEEP,
        "web_search": {
            "enabled": bool(ws.get("enabled", True)),
            "top_k": _int(ws.get("top_k", 5), 5),
            "cooldown_rounds": _int(ws.get("cooldown_rounds", 40), 40),
        },
        "ask_research": research_ask,
    }


def record_config(st):
    """配置变更留痕：相同哈希不重复记；变更写入 config_history 与研究日志。"""
    r = ensure_state(st)
    values = effective_config()
    sha = _canonical_hash(values)
    if r.get("config_sha256") == sha:
        return sha
    entry = {"at": _now(), "round": st.get("rounds", 0), "sha256": sha, "values": values}
    history = r.setdefault("config_history", [])
    history.append(entry)
    del history[:-config.RESEARCH_CONFIG_KEEP]
    r["config_sha256"] = sha
    _log(st, "config_recorded", sha256=sha, values=values)
    return sha


def _expression_terms(expression, limit=4):
    expr = _clean_topic(expression, 80)
    words = tokenizer.concept_words(expr, limit=limit)
    terms = [w for w, _ in words.most_common(limit)]
    if terms:
        return terms
    compact = re.sub(r"[\s，。；：、！？\"'“”‘’（）()\[\]{}]+", "", expr)
    return [compact[:24]] if compact else []


def _priority(st, kind, terms, extra=0.0):
    value = KIND_BASE.get(kind, 0.4) + extra
    active = {x.get("c") for x in (st.get("recent_active") or [])[-40:]
              if isinstance(x, dict)}
    if any(t and t in active for t in terms):
        value += 0.08
    kw = st.get("last_kw") or ""
    if any(t and t in kw for t in terms):
        value += 0.04
    return round(max(config.RESEARCH_PRIORITY_MIN,
                     min(config.RESEARCH_PRIORITY_MAX, value)), 3)


def _relation_parts(key):
    if not isinstance(key, str):
        return None
    parts = key.split("|")
    if len(parts) != 2:
        return None
    a, b = (p.strip() for p in parts)
    if not a or not b:
        return None
    return a, b


def _relation_key(key):
    parts = _relation_parts(key)
    if not parts:
        return str(key or "")
    return "|".join(sorted(parts))


def _task_key(kind, target):
    if target.get("type") == "relation":
        # 同一关系只保留一个任务：弱关系与预测落空共享 ID，后者可原地升级前者
        return f"relation|{_relation_key(target.get('relation'))}"
    if target.get("type") == "claim":
        return f"{kind}|claim|{target.get('claim_id')}"
    if target.get("type") == "concept":
        return f"{kind}|concept|{target.get('concept')}"
    return f"{kind}|exploration|{target.get('topic')}|{target.get('bucket', 0)}"


def _merged_terminal_status(tasks, has_materials, fallback):
    if has_materials:
        return "materials_found"
    statuses = {t.get("status") for t in tasks}
    for status in ("blocked", "exhausted", "abandoned"):
        if status in statuses:
            return status
    return fallback


def _planned_query_ids(r, task):
    out = []
    for qid in task.get("query_ids") or []:
        query = r["queries"].get(qid)
        if query and query.get("status") == "planned":
            out.append(qid)
    return out


def _cancel_planned_queries(r, qids):
    cancelled = []
    for qid in dict.fromkeys(qids or []):
        query = r["queries"].get(qid)
        if query and query.get("status") == "planned":
            query["status"] = "cancelled"
            query["failure"] = "superseded"
            cancelled.append(qid)
    return cancelled


def _record_kind_history(task, entry):
    history = [dict(x) for x in (task.get("kind_history") or [])
               if isinstance(x, dict)]
    history.append(dict(entry))
    kept, dropped = _bounded_history(history)
    task["kind_history"] = kept
    return dropped


def _record_merged_query_cancellation(task, qids, st):
    """种类没变但来源任务种类不同：把作废查询记到最近的同种类迁移记录上。

    返回是否新追加了一条 kind_history（False 表示复用已有迁移记录）。
    """
    history = [dict(x) for x in (task.get("kind_history") or [])
               if isinstance(x, dict)]
    appended = False
    for entry in reversed(history):
        if entry.get("to") == task.get("kind"):
            entry["cancelled_queries"] = list(dict.fromkeys(
                list(entry.get("cancelled_queries") or []) + list(qids)))
            break
    else:
        history.append({
            "round": st.get("rounds", 0),
            "from": task.get("kind"),
            "to": task.get("kind"),
            "reason": "迁移合并：作废不同种类来源任务的计划查询",
            "archived_attempts": 0,
            "archived_rewrites": 0,
            "cancelled_queries": list(qids),
        })
        appended = True
    kept, _ = _bounded_history(history)
    task["kind_history"] = kept
    return appended


def _merge_relation_task(primary, other):
    """迁移时合并同一关系的两条旧任务，历史尝试与关联不丢。

    primary 是生命周期载体（活跃/新建任务优先），其 attempts 保持为当前进度；
    其余任务的当前尝试与既有 attempt_history 一并迁入。attempt_history 与
    kind_history 都按轮次排序后只保留最近 HISTORY_KEEP 条。
    返回 (迁入的历史条数, 归档的当前尝试条数, 被截断的条数)。
    """
    other_history = other.get("attempt_history")
    other_history = other_history if isinstance(other_history, list) else []
    imported = 0
    history = [dict(x) for x in (primary.get("attempt_history") or [])
               if isinstance(x, dict)]
    for item in other_history:
        if isinstance(item, dict):
            history.append(dict(item))
            imported += 1
    archived = 0
    attempts = list(other.get("attempts") or [])
    if attempts:
        history.append({
            "kind": other.get("kind"),
            "round": other.get("updated_round", 0),
            "attempts": attempts,
            "rewrites": other.get("rewrites", 0),
            "result_count": other.get("result_count", 0),
        })
        archived += 1
    kept, dropped = _bounded_history(history)
    if kept:
        primary["attempt_history"] = kept
    kind_history = [dict(x) for x in (primary.get("kind_history") or [])
                    if isinstance(x, dict)]
    kind_history += [dict(x) for x in (other.get("kind_history") or [])
                     if isinstance(x, dict)]
    kept_kind, _ = _bounded_history(kind_history)
    if kept_kind:
        primary["kind_history"] = kept_kind
    primary["query_ids"] = list(dict.fromkeys(
        list(primary.get("query_ids") or []) + list(other.get("query_ids") or [])))
    primary["source_ids"] = list(dict.fromkeys(
        list(primary.get("source_ids") or []) + list(other.get("source_ids") or [])))
    primary["result_count"] = max(
        _int(primary.get("result_count"), 0) + _int(other.get("result_count"), 0),
        len(primary["source_ids"]))
    primary["priority"] = max(float(primary.get("priority") or 0.0),
                              float(other.get("priority") or 0.0))
    primary["updated_round"] = max(_int(primary.get("updated_round"), 0),
                                   _int(other.get("updated_round"), 0))
    primary["last_seen_round"] = max(_int(primary.get("last_seen_round"), 0),
                                     _int(other.get("last_seen_round"), 0))
    created = [x for x in (primary.get("created_round"),
                           other.get("created_round")) if isinstance(x, int)]
    if created:
        primary["created_round"] = min(created)
    if other.get("verification") == "pending_verification":
        primary["verification"] = "pending_verification"
    return imported, archived, dropped


def _repair_prior_kind_transitions(st, r):
    """v2→v3：修复旧迁移只换 kind 却没归档尝试/作废计划查询的遗留。

    只能依据 kind_history 的迁移轮次、查询创建轮次和当前 attempts 做可审计修复；
    kind_history 缺失的历史无法重建，不做猜测，只记录不可恢复项。
    返回 (修复任务数, 不可恢复任务数)。
    """
    repaired = 0
    unrecoverable = 0
    for task in r["tasks"].values():
        if not isinstance(task, dict):
            continue
        history = task.get("kind_history")
        if not isinstance(history, list):
            continue
        attempts = [a for a in (task.get("attempts") or []) if isinstance(a, dict)]
        last = None
        for entry in history:
            if (isinstance(entry, dict) and entry.get("to") == task.get("kind")
                    and entry.get("from") != entry.get("to")):
                last = entry
        if last is None:
            if history and attempts:
                unrecoverable += 1
                _log(st, "task_migration_unrecoverable", task_id=task.get("id"),
                     kind=task.get("kind"), reason="no_current_transition",
                     attempts=len(attempts),
                     planned_queries=len(_planned_query_ids(r, task)))
            continue
        since = _int(last.get("round"), 0)
        if since <= 0:
            if attempts:
                unrecoverable += 1
                _log(st, "task_migration_unrecoverable", task_id=task.get("id"),
                     kind=task.get("kind"), reason="invalid_transition_round",
                     attempts=len(attempts),
                     planned_queries=len(_planned_query_ids(r, task)))
            continue
        old_attempts = [a for a in attempts if _int(a.get("round"), 0) < since]
        current_attempts = [a for a in attempts
                            if _int(a.get("round"), 0) >= since]
        archived = 0
        dropped = 0
        if old_attempts:
            history_rows = [dict(x) for x in (task.get("attempt_history") or [])
                            if isinstance(x, dict)]
            history_rows.append({
                "kind": last.get("from"),
                "round": max((_int(a.get("round"), 0) for a in old_attempts),
                             default=since),
                "attempts": old_attempts,
                "rewrites": _int(task.get("rewrites"), 0),
                "result_count": _int(task.get("result_count"), 0),
                "repaired_by": f"schema-{RESEARCH_SCHEMA_VERSION}",
            })
            kept, dropped = _bounded_history(history_rows)
            task["attempt_history"] = kept
            task["attempts"] = current_attempts
            task["rewrites"] = 0
            archived = len(old_attempts)
        old_planned = []
        for qid in task.get("query_ids") or []:
            query = r["queries"].get(qid)
            if (query and query.get("status") == "planned"
                    and _int(query.get("created_round"), 0) < since):
                old_planned.append(qid)
        cancelled = _cancel_planned_queries(r, old_planned)
        if cancelled:
            last["cancelled_queries"] = list(dict.fromkeys(
                list(last.get("cancelled_queries") or []) + cancelled))
        if archived or cancelled:
            repaired += 1
            _log(st, "task_migration_repaired", task_id=task.get("id"),
                 kind=task.get("kind"), transition_round=since,
                 archived_attempts=archived, cancelled_queries=cancelled,
                 attempt_history_dropped=dropped)
    return repaired, unrecoverable


def _normalize_relation_tasks(st, r):
    """关系任务 ID 归一到关系键；同时按升级语义合并种类与查询/尝试预算。"""
    groups = {}
    for task_id, task in list(r["tasks"].items()):
        target = task.get("target") if isinstance(task, dict) else None
        if not isinstance(target, dict) or target.get("type") != "relation":
            continue
        new_id = _stable_id("RT", _task_key(task.get("kind"), target))
        groups.setdefault(new_id, []).append(task_id)
    processed = 0
    for new_id, ids in groups.items():
        tasks = [r["tasks"][task_id] for task_id in ids
                 if isinstance(r["tasks"].get(task_id), dict)]
        if not tasks:
            continue
        kind_source = max(tasks, key=lambda t: (
            KIND_BASE.get(t.get("kind"), 0.0), _int(t.get("updated_round"), 0)))
        live = [t for t in tasks if t.get("status") in ("open", "active")]
        # 生命周期与种类分开选：种类取最强信号，进度取仍可执行的那条
        carrier = max(live, key=lambda t: (
            t.get("status") == "active", _int(t.get("updated_round"), 0),
            float(t.get("priority") or 0.0))) if live else None
        primary = carrier or kind_source
        final_kind = kind_source.get("kind")
        carrier_changes = (carrier is not None
                           and primary.get("kind") != final_kind)
        old_kind = primary.get("kind")
        old_rewrites = _int(primary.get("rewrites"), 0)
        status_before = primary.get("status")
        own_history = sum(1 for x in (primary.get("attempt_history") or [])
                          if isinstance(x, dict))
        own_kind_history = sum(1 for x in (primary.get("kind_history") or [])
                               if isinstance(x, dict))
        # 先确定最终种类，再按来源任务种类收集必须作废的计划查询：
        # 载体换种类时它自己的计划查询都属于旧种类；其它任务只有种类不同才作废。
        old_planned = []
        for task in tasks:
            if task is primary:
                if carrier_changes:
                    old_planned.extend(_planned_query_ids(r, task))
                continue
            if task.get("kind") != final_kind:
                old_planned.extend(_planned_query_ids(r, task))
        old_planned = list(dict.fromkeys(old_planned))
        archived = 0
        archived_attempts = 0
        archived_rows = 0
        dropped = 0
        if carrier_changes:
            archived_attempts = len(primary.get("attempts") or [])
            archived_rows = archived_attempts
            if archived_attempts:
                history_rows = [dict(x) for x in
                                (primary.get("attempt_history") or [])
                                if isinstance(x, dict)]
                history_rows.append({
                    "kind": old_kind,
                    "round": _int(primary.get("updated_round"), 0),
                    "attempts": list(primary.get("attempts") or []),
                    "rewrites": old_rewrites,
                    "result_count": _int(primary.get("result_count"), 0),
                })
                kept, dropped = _bounded_history(history_rows)
                primary["attempt_history"] = kept
                archived = 1
            primary["attempts"] = []
            primary["rewrites"] = 0
        imported = 0
        kind_imported = 0
        for task in tasks:
            if task is primary:
                continue
            kind_imported += sum(1 for x in (task.get("kind_history") or [])
                                 if isinstance(x, dict))
            archived_rows += len(task.get("attempts") or [])
            imp, arc, drop = _merge_relation_task(primary, task)
            imported += imp
            archived += arc
            dropped += drop
        cancelled = _cancel_planned_queries(r, old_planned)
        kind_changed = primary.get("kind") != final_kind
        kind_added = 0
        if kind_changed:
            _record_kind_history(primary, {
                "round": st.get("rounds", 0),
                "from": old_kind,
                "to": final_kind,
                "from_priority": primary.get("priority"),
                "to_priority": kind_source.get("priority"),
                "reason": "迁移合并：同一关系采用更强信号的任务种类",
                "archived_attempts": archived_attempts,
                "archived_rewrites": old_rewrites,
                "cancelled_queries": cancelled,
                "previous_question": primary.get("question"),
                "previous_reason": primary.get("reason"),
                "previous_target": primary.get("target"),
            })
            kind_added += 1
            primary["kind"] = final_kind
            primary["kind_label"] = (kind_source.get("kind_label")
                                     or KIND_LABELS.get(final_kind, final_kind))
            primary["question"] = kind_source.get("question")
            primary["reason"] = kind_source.get("reason")
            primary["target"] = kind_source.get("target")
        elif cancelled:
            if _record_merged_query_cancellation(primary, cancelled, st):
                kind_added += 1
        has_materials = _has_materials(primary)
        if carrier is not None:
            primary["status"] = carrier.get("status")
        else:
            primary["status"] = _merged_terminal_status(
                tasks, has_materials, primary.get("status"))
        if has_materials:
            primary["verification"] = "pending_verification"
        old_ids = [task_id for task_id in ids if task_id != new_id]
        for task_id in ids:
            r["tasks"].pop(task_id, None)
        primary["id"] = new_id
        r["tasks"][new_id] = primary
        for old_id in old_ids:
            for query in r["queries"].values():
                if query.get("task_id") == old_id:
                    query["task_id"] = new_id
            for entry in r.get("sources") or []:
                if isinstance(entry, dict) and entry.get("task_id") == old_id:
                    entry["task_id"] = new_id
        final_history, extra_dropped = _bounded_history(
            primary.get("attempt_history") or [])
        if final_history or "attempt_history" in primary:
            primary["attempt_history"] = final_history
        dropped += extra_dropped
        kept = len(final_history)
        # kind_history 也按轮次有界保留；旧状态可能带着未截断的长历史进入迁移
        final_kinds, extra_kind_dropped = _bounded_history(
            primary.get("kind_history") or [])
        if final_kinds or "kind_history" in primary:
            primary["kind_history"] = final_kinds
        kind_kept = len(final_kinds)
        kind_dropped = max(0, own_kind_history + kind_imported
                           + kind_added - kind_kept)
        status_changed = primary.get("status") != status_before
        if (old_ids or kind_changed or cancelled or imported or archived
                or dropped or kind_dropped or status_changed):
            _log(st, "task_id_migrated", old_ids=old_ids, new_id=new_id,
                 relation=(primary.get("target") or {}).get("relation"),
                 kind=primary.get("kind"), merged=len(ids) > 1,
                 kind_changed=kind_changed, cancelled_queries=cancelled,
                 query_ids=len(primary.get("query_ids") or []),
                 source_ids=len(primary.get("source_ids") or []),
                 attempt_history_own=own_history,
                 attempt_history_imported=imported,
                 attempts_archived=archived,
                 attempt_rows_archived=archived_rows,
                 attempt_history_kept=kept,
                 attempt_history_dropped=dropped,
                 kind_history_own=own_kind_history,
                 kind_history_imported=kind_imported,
                 kind_history_added=kind_added,
                 kind_history_kept=kind_kept,
                 kind_history_dropped=kind_dropped)
            if dropped:
                _log(st, "attempt_history_truncated", task_id=new_id,
                     dropped=dropped, keep=HISTORY_KEEP)
            if kind_dropped:
                _log(st, "kind_history_truncated", task_id=new_id,
                     dropped=kind_dropped, keep=HISTORY_KEEP)
            processed += 1
    return processed


def _migrate_legacy_state(st, r):
    """1/2 状态 → 3：ID 归一、来源字段更名、迁移升级语义与 v2 审计修复。

    查询与来源索引中的 task_id 同步改写；历史 JSONL 不重写（append-only）。
    """
    from_version = _int(r.get("schema_version"), 1)
    renamed = 0
    for entry in r.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        legacy = entry.pop("content_sha256", None)
        if legacy is not None:
            renamed += 1
            if not entry.get("title_excerpt_sha256"):
                entry["title_excerpt_sha256"] = legacy
    if renamed:
        _log(st, "source_fields_migrated", count=renamed,
             field="title_excerpt_sha256")
    repaired, unrecoverable = _repair_prior_kind_transitions(st, r)
    processed = _normalize_relation_tasks(st, r)
    _log(st, "research_schema_migrated", from_version=from_version,
         to_version=RESEARCH_SCHEMA_VERSION, tasks_repaired=repaired,
         tasks_unrecoverable=unrecoverable,
         relation_groups_updated=processed, source_fields_renamed=renamed)


def _update_miss_backlog(st, r):
    """把扫描窗口内的落空预测补录到状态里；窗口淘汰后仍能进入候选。"""
    backlog = r.setdefault("miss_backlog", {})
    added = 0
    preds = (st.get("edge_preds") or [])[-config.RESEARCH_SCAN_LIMIT:]
    for p in preds:
        if not isinstance(p, dict) or p.get("status") != "miss":
            continue
        a, b = p.get("a"), p.get("b")
        if not a or not b:
            continue
        key = "|".join(sorted([str(a), str(b)]))
        if key in backlog:
            continue
        backlog[key] = {
            "relation": key,
            "concepts": [a, b],
            "round": p.get("round"),
            "settled_round": p.get("settled_round"),
            "recorded_round": st.get("rounds", 0),
        }
        added += 1
    if added:
        _log(st, "miss_backlog_recorded", count=added)
    excess = len(backlog) - config.RESEARCH_MISS_BACKLOG
    if excess > 0:
        ordered = sorted(
            backlog.items(),
            key=lambda kv: (_int(kv[1].get("settled_round"), 0),
                            _int(kv[1].get("round"), 0)))
        removed = [key for key, _ in ordered[:excess]]
        for key in removed:
            backlog.pop(key, None)
        _log(st, "miss_backlog_evicted", count=len(removed),
             relations=removed[:20])


def _consume_miss_backlog(r):
    """落空信号已被 prediction_miss 任务吸收后移出补录，避免永久占位。"""
    backlog = r.get("miss_backlog")
    if not isinstance(backlog, dict):
        return
    for key in list(backlog):
        task_id = _stable_id("RT", _task_key(
            "prediction_miss", {"type": "relation", "relation": key}))
        task = r["tasks"].get(task_id)
        if task and task.get("kind") == "prediction_miss":
            backlog.pop(key, None)


def _prediction_candidates(st):
    out = []
    r = st.get("research") if isinstance(st.get("research"), dict) else {}
    entries = {}
    backlog = r.get("miss_backlog")
    if isinstance(backlog, dict):
        for key, item in backlog.items():
            if (isinstance(item, dict)
                    and isinstance(item.get("concepts"), list)
                    and len(item["concepts"]) >= 2):
                entries[key] = item
    # 最近窗口内的落空优先用于候选；更早的由 miss_backlog 补齐
    for p in (st.get("edge_preds") or [])[-60:]:
        if not isinstance(p, dict) or p.get("status") != "miss":
            continue
        a, b = p.get("a"), p.get("b")
        if not a or not b:
            continue
        key = "|".join(sorted([str(a), str(b)]))
        entries[key] = {
            "relation": key, "concepts": [a, b], "round": p.get("round"),
            "settled_round": p.get("settled_round"),
        }
    for key, item in sorted(
            entries.items(),
            key=lambda kv: -_int(kv[1].get("settled_round"), 0)):
        a, b = item["concepts"][:2]
        target = {"type": "relation", "relation": key, "concepts": [a, b],
                  "origin": "prediction_miss",
                  "prediction_round": item.get("round"),
                  "settled_round": item.get("settled_round")}
        out.append({
            "kind": "prediction_miss",
            "target": target,
            "question": f"《{a}》和《{b}》之间缺少什么中间概念或条件？为什么它们从未同现？",
            "reason": (f"预测落空：第 {item.get('round')} 轮下注《{a}》↔《{b}》，"
                       f"第 {item.get('settled_round')} 轮结算时从未同现。"),
            "priority": _priority(st, "prediction_miss", [a, b]),
        })
    return out


def _claim_candidates(st):
    out = []
    claims = st.get("claims") or {}
    items = list(claims.items())[-config.RESEARCH_SCAN_LIMIT:]
    for cid, claim in reversed(items):
        if not isinstance(claim, dict) or claim.get("status") == "refuted":
            continue
        if claim.get("status") not in (None, "hypothesis", "believed"):
            continue
        support = _int(claim.get("independent_support", claim.get("support", 0)), 0)
        counter = _int(claim.get("independent_counter", 0), 0)
        if support or counter:
            continue
        expression = _clean_topic(claim.get("expression") or cid, 80)
        if not expression:
            continue
        terms = _expression_terms(expression)
        target = {"type": "claim", "claim_id": cid, "expression": expression,
                  "concepts": terms, "claim_status": claim.get("status")}
        out.append({
            "kind": "claim_evidence_gap",
            "target": target,
            "question": f"有哪些独立资料能支持或反驳「{expression[:48]}」？",
            "reason": (f"命题证据缺口：独立支持 {support}、独立反证 {counter}"
                       f"（当前状态：{claim.get('status') or 'hypothesis'}）。"),
            "priority": _priority(st, "claim_evidence_gap", terms),
        })
    return out


def _relation_candidates(st):
    out = []
    relations = st.get("relations") or {}
    items = list(relations.items())[-config.RESEARCH_SCAN_LIMIT:]
    for key, rel in reversed(items):
        if not isinstance(rel, dict):
            continue
        parts = _relation_parts(key)
        if not parts:
            continue
        a, b = parts
        n = _int(rel.get("n", 0), 0)
        w = _int(rel.get("w", 0), 0)
        if n > 1 or w > 2:
            continue
        rel_key = "|".join(sorted([a, b]))
        rel_type = rel.get("type")
        target = {"type": "relation", "relation": rel_key, "concepts": [a, b],
                  "w": w, "n": n, "rel_type": rel_type}
        out.append({
            "kind": "relation_unverified",
            "target": target,
            "question": f"《{a}》和《{b}》是什么关系？有哪些独立来源能证实或反驳这条边？",
            "reason": (f"弱关系未验证：目前只由同现建边（w={w}, n={n}），"
                       f"没有独立来源或探针证据。"),
            "priority": _priority(st, "relation_unverified", [a, b],
                                  0.03 if rel_type else 0.0),
        })
    return out


def _isolated_candidates(st):
    out = []
    relations = st.get("relations") or {}
    concepts = st.get("concepts") or {}
    degree = Counter()
    for key in relations:
        parts = _relation_parts(key)
        if parts:
            degree[parts[0]] += 1
            degree[parts[1]] += 1
    items = list(concepts.items())[-config.RESEARCH_SCAN_LIMIT:]
    for name, meta in reversed(items):
        if not name or len(name) < 2 or not isinstance(meta, dict):
            continue
        n = _int(meta.get("n", 0), 0)
        if n < 3 or degree.get(name, 0) != 0:
            continue
        target = {"type": "concept", "concept": name, "concepts": [name], "n": n}
        out.append({
            "kind": "isolated_concept",
            "target": target,
            "question": f"《{name}》通常和什么概念一起出现？它成立需要什么条件？",
            "reason": f"概念孤立：已晋升（出现 {n} 次）但没有任何关系边，无法参与推理。",
            "priority": _priority(st, "isolated_concept", [name]),
        })
    return out


def _task_candidates(st):
    # 不在生成阶段按种类截断：旧任务不能永久占用“新候选”额度；
    # 新建由优先级 + 每轮预算约束，扫描范围仍受 RESEARCH_SCAN_LIMIT 限制。
    return (_prediction_candidates(st) + _claim_candidates(st)
            + _relation_candidates(st) + _isolated_candidates(st))


def _archive_task(st, r, task, reason):
    """容量归档：保留待验证材料所需的任务快照，查询/来源关联可恢复。"""
    source_ids = set(task.get("source_ids") or [])
    entry = {
        "id": task.get("id"),
        "kind": task.get("kind"),
        "kind_label": task.get("kind_label"),
        "question": task.get("question"),
        "reason": task.get("reason"),
        "target": task.get("target"),
        "priority": task.get("priority"),
        "status": task.get("status"),
        "verification": task.get("verification"),
        "created_round": task.get("created_round"),
        "updated_round": task.get("updated_round"),
        "last_seen_round": task.get("last_seen_round"),
        "attempts": list(task.get("attempts") or []),
        "attempt_history": list(task.get("attempt_history") or []),
        "kind_history": list(task.get("kind_history") or []),
        "query_ids": list(task.get("query_ids") or []),
        "source_ids": list(task.get("source_ids") or []),
        "result_count": task.get("result_count", 0),
        "rewrites": task.get("rewrites", 0),
        "config_sha256": task.get("config_sha256"),
        "archived_at": _now(),
        "archived_round": st.get("rounds", 0),
        "archive_reason": reason,
        # 即使查询/来源索引日后被容量淘汰，归档仍能还原关联
        "queries": [
            {"id": q.get("id"), "purpose": q.get("purpose"),
             "text": q.get("text"), "status": q.get("status"),
             "result_count": q.get("result_count", 0)}
            for qid in task.get("query_ids") or []
            for q in [r["queries"].get(qid)] if isinstance(q, dict)
        ],
        "sources": [
            {"id": s.get("id"), "url": s.get("url"), "title": s.get("title"),
             "task_id": s.get("task_id"), "query_id": s.get("query_id"),
             "verification": s.get("verification")}
            for s in (r.get("sources") or [])
            if isinstance(s, dict) and s.get("id") in source_ids
        ],
    }
    archive = r.setdefault("archived_tasks", [])
    archive.append(entry)
    excess = len(archive) - config.RESEARCH_ARCHIVE_KEEP
    if excess > 0:
        del archive[:-config.RESEARCH_ARCHIVE_KEEP]
        _log(st, "task_archive_evicted", count=excess,
             keep=config.RESEARCH_ARCHIVE_KEEP)
    r["stats"]["tasks_archived"] = r["stats"].get("tasks_archived", 0) + 1
    _log(st, "task_archived", task_id=task.get("id"),
         status=task.get("status"), reason=reason, archived=True,
         kind=task.get("kind"), verification=task.get("verification"),
         query_ids=entry["query_ids"], source_ids=entry["source_ids"])


def _drop_task(st, r, task, reason):
    if _has_materials(task):
        _archive_task(st, r, task, reason)
    else:
        _log(st, "task_archived", task_id=task.get("id"),
             status=task.get("status"), reason=reason, archived=False,
             source_ids=list(task.get("source_ids") or [])[:20])
    r["tasks"].pop(task.get("id"), None)


def _prune_tasks(st, r):
    tasks = r["tasks"]
    # open/active 预算：正常流程不会超；旧状态或异常时按优先级保留
    viable = sorted([t for t in tasks.values()
                     if t.get("status") in ("open", "active")],
                    key=lambda t: t.get("priority", 0.0))
    excess = len(viable) - config.RESEARCH_MAX_TASKS
    for task in viable[:max(0, excess)]:
        _drop_task(st, r, task, "open_cap")
    excess = len(tasks) - config.RESEARCH_TASK_KEEP
    if excess <= 0:
        return
    closed = sorted([t for t in tasks.values() if t.get("status") in CLOSED],
                    key=lambda t: t.get("updated_round", 0))
    for task in closed:
        if len(tasks) <= config.RESEARCH_TASK_KEEP:
            break
        _drop_task(st, r, task, "closed_cap")
    if len(tasks) > config.RESEARCH_TASK_KEEP:
        # 待验证材料不直接丢弃：_drop_task 会写入 archived_tasks（含查询/来源快照）
        old_material = sorted(
            [t for t in tasks.values() if t.get("status") == "materials_found"],
            key=lambda t: t.get("updated_round", 0))
        for task in old_material:
            if len(tasks) <= config.RESEARCH_TASK_KEEP:
                break
            _drop_task(st, r, task, "material_cap")
    while len(tasks) > config.RESEARCH_TASK_KEEP:
        task = min(tasks.values(),
                   key=lambda t: (t.get("priority", 0.0), t.get("updated_round", 0)))
        _drop_task(st, r, task, "hard_cap")


def _new_tasks_this_round(st, r):
    round_now = st.get("rounds", 0)
    return sum(1 for t in r["tasks"].values()
               if t.get("created_round") == round_now)


def _creation_block_reason(st, r):
    if len(r["tasks"]) >= config.RESEARCH_TASK_KEEP:
        return "total_cap"
    open_active = sum(1 for t in r["tasks"].values()
                      if t.get("status") in ("open", "active"))
    if open_active >= config.RESEARCH_MAX_TASKS:
        return "open_cap"
    if _new_tasks_this_round(st, r) >= config.RESEARCH_NEW_PER_ROUND:
        return "round_cap"
    return None


def _create_task(st, r, candidate, log_blocked=True):
    # 所有入口（含探索回退）共用同一组硬上限
    blocked = _creation_block_reason(st, r)
    if blocked:
        if log_blocked:
            _log(st, "task_creation_blocked", kind=candidate["kind"],
                 target=candidate["target"], reason=blocked,
                 round_new=_new_tasks_this_round(st, r))
        return None
    task_id = _stable_id("RT", _task_key(candidate["kind"], candidate["target"]))
    task = {
        "id": task_id,
        "kind": candidate["kind"],
        "kind_label": KIND_LABELS.get(candidate["kind"], candidate["kind"]),
        "question": candidate["question"],
        "reason": candidate["reason"],
        "target": candidate["target"],
        "priority": candidate["priority"],
        "status": "open",
        "verification": "unverified",
        "created_round": st.get("rounds", 0),
        "updated_round": st.get("rounds", 0),
        "last_seen_round": st.get("rounds", 0),
        "attempts": [],
        "query_ids": [],
        "source_ids": [],
        "result_count": 0,
        "rewrites": 0,
        "config_sha256": r.get("config_sha256"),
    }
    r["tasks"][task_id] = task
    r["stats"]["tasks_created"] = r["stats"].get("tasks_created", 0) + 1
    _log(st, "task_created", task_id=task_id, kind=task["kind"],
         question=task["question"], reason=task["reason"],
         priority=task["priority"], target=task["target"])
    return task


def _upgrade_task(st, r, task, candidate):
    """同一关系出现更强信号：原地升级，保留查询/来源与旧尝试历史。"""
    old = {
        "kind": task.get("kind"),
        "priority": task.get("priority"),
        "question": task.get("question"),
        "reason": task.get("reason"),
        "target": task.get("target"),
        "rewrites": task.get("rewrites", 0),
    }
    archived = list(task.get("attempts") or [])
    history_dropped = 0
    if archived:
        history = [dict(x) for x in (task.get("attempt_history") or [])
                   if isinstance(x, dict)]
        history.append({
            "kind": old["kind"],
            "round": task.get("updated_round", 0),
            "attempts": archived,
            "rewrites": old["rewrites"],
            "result_count": task.get("result_count", 0),
        })
        kept, history_dropped = _bounded_history(history)
        task["attempt_history"] = kept
    cancelled = _cancel_planned_queries(r, task.get("query_ids") or [])
    task["kind"] = candidate["kind"]
    task["kind_label"] = KIND_LABELS.get(candidate["kind"], candidate["kind"])
    task["question"] = candidate["question"]
    task["reason"] = candidate["reason"]
    task["target"] = candidate["target"]
    task["priority"] = candidate["priority"]
    task["status"] = "open"
    task["attempts"] = []
    task["rewrites"] = 0
    task["verification"] = ("pending_verification"
                            if _has_materials(task) else "unverified")
    task["updated_round"] = st.get("rounds", 0)
    kind_dropped = _record_kind_history(task, {
        "round": st.get("rounds", 0),
        "from": old["kind"],
        "to": candidate["kind"],
        "from_priority": old["priority"],
        "to_priority": candidate["priority"],
        "reason": candidate["reason"],
        "archived_attempts": len(archived),
        "archived_rewrites": old["rewrites"],
        "cancelled_queries": cancelled,
        "previous_question": old["question"],
        "previous_reason": old["reason"],
        "previous_target": old["target"],
    })
    if history_dropped:
        _log(st, "attempt_history_truncated", task_id=task["id"],
             dropped=history_dropped, keep=HISTORY_KEEP)
    if kind_dropped:
        _log(st, "kind_history_truncated", task_id=task["id"],
             dropped=kind_dropped, keep=HISTORY_KEEP)
    _log(st, "task_upgraded", task_id=task["id"],
         from_kind=old["kind"], to_kind=candidate["kind"],
         from_priority=old["priority"], to_priority=candidate["priority"],
         archived_attempts=len(archived), cancelled_queries=cancelled,
         reason=candidate["reason"])


def _refresh_task(st, task, candidate):
    task["last_seen_round"] = st.get("rounds", 0)
    old_kind = task.get("kind")
    new_kind = candidate["kind"]
    if (old_kind in RELATION_KINDS and new_kind in RELATION_KINDS
            and KIND_BASE.get(new_kind, 0.0) > KIND_BASE.get(old_kind, 0.0)):
        _upgrade_task(st, ensure_state(st), task, candidate)
        return
    if task.get("status") not in ("open", "active"):
        return
    if old_kind in RELATION_KINDS:
        new_priority = max(task.get("priority", 0.0), candidate["priority"])
    else:
        new_priority = candidate["priority"]
    if abs(new_priority - task.get("priority", 0.0)) >= 0.02:
        old = task.get("priority")
        task["priority"] = new_priority
        _log(st, "task_priority_updated", task_id=task["id"],
             old=old, new=task["priority"], reason=candidate["reason"])


def ensure_tasks(st):
    """从本地状态刷新研究任务；不产生网络/教师/LLM 调用。"""
    r = ensure_state(st)
    record_config(st)
    _update_miss_backlog(st, r)
    _prune_tasks(st, r)
    tasks = r["tasks"]

    creation_blocked = False
    for candidate in sorted(_task_candidates(st),
                            key=lambda c: (-c["priority"], c["kind"])):
        task_id = _stable_id("RT", _task_key(candidate["kind"], candidate["target"]))
        existing = tasks.get(task_id)
        if existing is not None:
            _refresh_task(st, existing, candidate)
            continue
        # 预算只约束新建：被拒后继续扫描，后面的既有任务仍可刷新/升级
        if _create_task(st, r, candidate,
                        log_blocked=not creation_blocked) is None:
            creation_blocked = True
    _prune_tasks(st, r)
    _consume_miss_backlog(r)
    return tasks


def _viable_tasks(st):
    r = ensure_state(st)
    items = [t for t in r["tasks"].values() if t.get("status") in ("open", "active")]
    items.sort(key=lambda t: (-t.get("priority", 0.0), t.get("created_round", 0)))
    return items


def top_open_task(st):
    """只读：工作记忆/决策层调用，不得为了读取而给状态添字段。"""
    r = st.get("research")
    if not isinstance(r, dict) or not isinstance(r.get("tasks"), dict):
        return None
    items = [t for t in r["tasks"].values() if t.get("status") in ("open", "active")]
    items.sort(key=lambda t: (-t.get("priority", 0.0), t.get("created_round", 0)))
    return items[0] if items else None


def _terms_for_task(task):
    target = task.get("target") or {}
    if target.get("type") == "claim":
        terms = list(target.get("concepts") or [])
        if not terms:
            terms = _expression_terms(target.get("expression") or "")
        return terms
    return [t for t in (target.get("concepts") or []) if t]


def plan_queries(task):
    """按任务类型生成不同目的的查询；不在计划阶段读写状态。"""
    kind = task.get("kind")
    target = task.get("target") or {}
    terms = _terms_for_task(task)
    specs = []
    if kind == "prediction_miss":
        a, b = (list(target.get("concepts") or []) + ["", ""])[:2]
        specs = [
            ("bridge", f"{a}和{b}有什么联系"),
            ("mechanism", f"{a}和{b}为什么相关 机制"),
            ("relation", f"{a}和{b}的关系"),
        ]
    elif kind == "claim_evidence_gap":
        expression = _clean_topic(target.get("expression") or "", 36)
        head = terms[0] if terms else expression
        specs = [
            ("evidence", f"{expression} 支持证据"),
            ("counterexample", f"{expression} 反例 不成立"),
            ("definition", f"{head}是什么"),
        ]
    elif kind == "relation_unverified":
        a, b = (list(target.get("concepts") or []) + ["", ""])[:2]
        specs = [
            ("relation", f"{a}和{b}有什么关系"),
            ("evidence", f"{a}和{b} 相关证据"),
            ("counterexample", f"{a}和{b} 无关的反例"),
        ]
    elif kind in ("isolated_concept", "exploration"):
        head = terms[0] if terms else _clean_topic(target.get("topic") or "", 24)
        specs = [
            ("definition", f"{head}是什么"),
            ("context", f"{head} 相关概念 例子"),
            ("mechanism", f"{head} 原理 为什么"),
        ]
    out, seen = [], set()
    for purpose, text in specs:
        text = _clean_topic(text, 60)
        if not text or any(bw in text for bw in config.SEARCH_BLACK_WORDS):
            continue
        norm = normalize_query(text)
        if norm in seen:
            continue
        seen.add(norm)
        out.append({"purpose": purpose, "text": text})
        if len(out) >= config.RESEARCH_QUERIES_PER_TASK:
            break
    return out


def normalize_query(text):
    return re.sub(r"\s+", "", str(text or "").lower())


def _shingles(text):
    norm = re.sub(r"[^\w\u4e00-\u9fff]", "", normalize_query(text))
    if len(norm) <= 2:
        return {norm} if norm else set()
    return {norm[i:i + 2] for i in range(len(norm) - 1)}


def duplicate_reason(st, text, exclude_query_id=None):
    r = ensure_state(st)
    norm = normalize_query(text)
    if not norm:
        return None
    sh = _shingles(text)
    for item in reversed(r["recent_queries"][-config.RESEARCH_QUERY_HISTORY:]):
        if not isinstance(item, dict):
            continue
        if exclude_query_id and item.get("query_id") == exclude_query_id:
            continue
        if item.get("normalized") == norm:
            return {"kind": "exact", "query_id": item.get("query_id"),
                    "text": item.get("text"), "score": 1.0}
        other = _shingles(item.get("text") or "")
        if not sh or not other:
            continue
        score = len(sh & other) / len(sh | other)
        if score >= config.RESEARCH_DUP_JACCARD:
            return {"kind": "similar", "query_id": item.get("query_id"),
                    "text": item.get("text"), "score": round(score, 3)}
    return None


def _create_query(st, r, task, purpose, text, selection_reason,
                  rewrite_of=None, rewrite_reason=None):
    qid = _stable_id("RQ", f"{task['id']}|{purpose}|{normalize_query(text)}|"
                           f"{len(task.get('query_ids') or [])}")
    query = {
        "id": qid,
        "task_id": task["id"],
        "purpose": purpose,
        "purpose_label": PURPOSE_LABELS.get(purpose, purpose),
        "text": text,
        "normalized": normalize_query(text),
        "selection_reason": selection_reason,
        "status": "planned",
        "created_round": st.get("rounds", 0),
        "used_round": None,
        "result_count": 0,
        "failure": None,
        "rewrite_of": rewrite_of,
        "rewrite_reason": rewrite_reason,
        "sources": [],
        "config_sha256": r.get("config_sha256"),
    }
    r["queries"][qid] = query
    if len(r["queries"]) > config.RESEARCH_QUERY_KEEP:
        for old_id in list(r["queries"])[:-config.RESEARCH_QUERY_KEEP]:
            r["queries"].pop(old_id, None)
    task.setdefault("query_ids", []).append(qid)
    r["stats"]["queries_planned"] = r["stats"].get("queries_planned", 0) + 1
    _log(st, "query_planned", query_id=qid, task_id=task["id"], purpose=purpose,
         text=text, selection_reason=selection_reason,
         rewrite_of=rewrite_of)
    return query


def _task_query_normalized(task, r):
    """只把真正执行过的查询计入“已用文本”。

    已作废/仍待执行的查询不占用文本额度，否则升级后的新种类无法复用同一问题。
    """
    out = set()
    for qid in task.get("query_ids") or []:
        query = r["queries"].get(qid)
        if not query or not query.get("normalized"):
            continue
        if query.get("status") not in EXECUTED_QUERY_STATUSES:
            continue
        out.add(query["normalized"])
    return out


def _next_for_task(st, r, task):
    if len(task.get("attempts") or []) >= config.RESEARCH_ATTEMPTS_PER_TASK:
        _finalize(st, task)
        return None
    used_norm = _task_query_normalized(task, r)
    planned = []
    for qid in task.get("query_ids") or []:
        query = r["queries"].get(qid)
        if query and query.get("status") == "planned":
            planned.append(query)
    planned.sort(key=lambda q: (0 if q.get("rewrite_of") else 1,
                                q.get("created_round", 0)))
    for query in planned:
        qid = query["id"]
        dup = duplicate_reason(st, query.get("text") or "", exclude_query_id=qid)
        if dup:
            query["status"] = "failed"
            query["failure"] = "duplicate"
            r["stats"]["duplicates_blocked"] = r["stats"].get("duplicates_blocked", 0) + 1
            _log(st, "query_duplicate_blocked", query_id=qid, task_id=task["id"],
                 text=query.get("text"), reason="planned_query_now_duplicate",
                 matched_query_id=dup.get("query_id"))
            continue
        return query
    for spec in plan_queries(task):
        norm = normalize_query(spec["text"])
        if norm in used_norm:
            continue
        dup = duplicate_reason(st, spec["text"])
        if dup:
            r["stats"]["duplicates_blocked"] = r["stats"].get("duplicates_blocked", 0) + 1
            _log(st, "query_duplicate_blocked", task_id=task["id"],
                 text=spec["text"], reason=dup.get("kind"),
                 matched_query_id=dup.get("query_id"), score=dup.get("score"))
            continue
        return _create_query(
            st, r, task, spec["purpose"], spec["text"],
            f"{KIND_LABELS.get(task.get('kind'), task.get('kind'))}：{spec['purpose']}角度")
    _finalize(st, task)
    return None


def _rewrite_spec(task, original):
    terms = _terms_for_task(task)
    if len(terms) >= 2:
        base = f"{terms[0]}和{terms[1]}"
    else:
        base = terms[0] if terms else _clean_topic(original.get("text") or "", 24)
    if not base:
        return None
    candidates = [f"{base} 综述", f"{base} 案例", f"{base} 教程"]
    for text in candidates:
        text = _clean_topic(text, 60)
        if text and normalize_query(text) != original.get("normalized"):
            return {"purpose": "broadened", "text": text}
    return None


def _plan_rewrite(st, r, task, original, failure_kind):
    if failure_kind != "no_results":
        return None
    if task.get("rewrites", 0) >= config.RESEARCH_MAX_REWRITES:
        return None
    if len(task.get("attempts") or []) >= config.RESEARCH_ATTEMPTS_PER_TASK:
        return None
    spec = _rewrite_spec(task, original)
    if not spec:
        return None
    dup = duplicate_reason(st, spec["text"])
    if dup:
        r["stats"]["duplicates_blocked"] = r["stats"].get("duplicates_blocked", 0) + 1
        _log(st, "query_duplicate_blocked", task_id=task["id"], text=spec["text"],
             reason=f"rewrite_{dup.get('kind')}",
             matched_query_id=dup.get("query_id"))
        return None
    query = _create_query(
        st, r, task, spec["purpose"], spec["text"],
        f"上次查询无结果，改用更宽角度（{original.get('purpose')}）",
        rewrite_of=original.get("id"), rewrite_reason=failure_kind)
    task["rewrites"] = task.get("rewrites", 0) + 1
    r["stats"]["rewrites_planned"] = r["stats"].get("rewrites_planned", 0) + 1
    _log(st, "query_rewrite_planned", task_id=task["id"],
         old_query_id=original.get("id"), new_query_id=query["id"],
         reason=failure_kind, old_text=original.get("text"), new_text=query["text"])
    return query


def _finalize(st, task):
    if task.get("status") in CLOSED or task.get("status") == "materials_found":
        return task.get("status")
    status = "materials_found" if _has_materials(task) else "exhausted"
    task["status"] = status
    task["updated_round"] = st.get("rounds", 0)
    if status == "materials_found":
        task["verification"] = "pending_verification"
    _log(st, "task_finalized", task_id=task["id"], status=status,
         attempts=len(task.get("attempts") or []),
         result_count=task.get("result_count", 0),
         source_ids=list(task.get("source_ids") or [])[:20])
    return status


def _ensure_exploration_task(st, topic):
    r = ensure_state(st)
    bucket = st.get("rounds", 0) // 200
    target = {"type": "exploration", "topic": topic, "concepts": [topic],
              "bucket": bucket}
    candidate = {
        "kind": "exploration",
        "target": target,
        "question": f"《{topic}》值得从哪个具体问题开始查？它缺的是什么信息？",
        "reason": "任务队列没有可继续的研究缺口，回退到有界探索性检索。",
        "priority": _priority(st, "exploration", [topic]),
    }
    task_id = _stable_id("RT", _task_key("exploration", target))
    task = r["tasks"].get(task_id)
    if task is None:
        task = _create_task(st, r, candidate)
    return task


def next_query(st, fallback_topic=None):
    """返回下一条待执行查询（planned），并保证任务/目的/去重均可追溯。"""
    ensure_tasks(st)
    r = ensure_state(st)
    for task in _viable_tasks(st):
        query = _next_for_task(st, r, task)
        if query:
            return query
    topic = _clean_topic(fallback_topic, 24)
    if topic:
        task = _ensure_exploration_task(st, topic)
        if task is None:
            return None
        query = _next_for_task(st, r, task)
        if query:
            return query
    return None


def _record_sources(st, r, task, query, results, serp_url, serp_sha256, fetched_at):
    ids = []
    seen_urls = set()
    rejected = 0
    index = r.setdefault("sources", [])
    by_id = {s.get("id"): s for s in index if isinstance(s, dict)}
    for item in results:
        if not isinstance(item, dict):
            rejected += 1
            continue
        url = str(item.get("url") or "").strip()
        if not is_valid_source_url(url):
            rejected += 1
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = str(item.get("title") or "")[:160]
        excerpt = str(item.get("snippet") or "")[:400]
        # 只覆盖 SERP 条目的标题+摘要，不是网页正文；正文提取属于阶段 3
        item_sha = hashlib.sha256(
            (title + "\n" + excerpt).encode("utf-8", errors="ignore")).hexdigest()
        sid = _stable_id("RS", f"{url}|{item_sha}")
        record = {
            "format": SOURCE_FORMAT,
            "id": sid,
            "task_id": task["id"],
            "query_id": query["id"],
            "purpose": query.get("purpose"),
            "url": url,
            "title": title,
            "excerpt": excerpt,
            "title_excerpt_sha256": item_sha,
            "fetched_at": fetched_at,
            "serp_url": serp_url,
            "serp_sha256": serp_sha256,
            "source_type": "web_runtime",
            "verification": "unverified",
            "config_sha256": r.get("config_sha256"),
        }
        append_jsonl("research_sources.jsonl", record)
        ids.append(sid)
        existing = by_id.get(sid)
        if existing:
            existing["fetch_count"] = existing.get("fetch_count", 1) + 1
            existing["last_fetched_at"] = fetched_at
            existing["last_task_id"] = task["id"]
            existing["last_query_id"] = query["id"]
            existing["last_serp_url"] = serp_url
        else:
            entry = {
                "id": sid, "task_id": task["id"], "query_id": query["id"],
                "purpose": query.get("purpose"),
                "url": url, "title": title, "title_excerpt_sha256": item_sha,
                "fetched_at": fetched_at, "serp_url": serp_url,
                "serp_sha256": serp_sha256,
                "source_type": "web_runtime", "verification": "unverified",
                "config_sha256": r.get("config_sha256"),
                "fetch_count": 1,
            }
            index.append(entry)
            by_id[sid] = entry
    if rejected:
        r["stats"]["source_candidates_rejected"] = (
            r["stats"].get("source_candidates_rejected", 0) + rejected)
        _log(st, "source_candidates_rejected", task_id=task["id"],
             query_id=query["id"], count=rejected,
             reason="url_missing_relative_or_non_http")
    del index[:-config.RESEARCH_SOURCE_KEEP]
    query["sources"] = list(dict.fromkeys((query.get("sources") or []) + ids))
    task["source_ids"] = list(dict.fromkeys((task.get("source_ids") or []) + ids))[-50:]
    r["stats"]["sources_recorded"] = r["stats"].get("sources_recorded", 0) + len(ids)
    if ids:
        _log(st, "sources_recorded", task_id=task["id"], query_id=query["id"],
             count=len(ids), source_ids=ids, serp_url=serp_url,
             serp_sha256=serp_sha256, verification="unverified")
    return ids


def record_execution(st, query, ok, results=None, failure_kind=None, detail="",
                     serp_url=None, serp_sha256=None, fetched_at=None):
    """执行结果入账：成功只登记候选材料，不写 claims/证据，不晋升任何知识。

    成功与否以“实际写入来源账本的有效来源数”为准：空 URL、相对链接、非
    HTTP(S) 协议都不算材料，不能把任务推进到 pending_verification。
    """
    r = ensure_state(st)
    qid = (query or {}).get("id")
    q = r["queries"].get(qid)
    if q is None:
        return None
    task = r["tasks"].get(q.get("task_id"))
    if task is None:
        return None
    results = list(results or [])
    fetched_at = fetched_at or _now()
    ids = _record_sources(st, r, task, q, results, serp_url, serp_sha256,
                          fetched_at) if ok else []
    ok = bool(ids)
    if ok:
        failure_kind = None
    elif not failure_kind:
        failure_kind = "no_results" if not results else "invalid_results"
    q["used_round"] = st.get("rounds", 0)
    q["result_count"] = len(ids)
    q["serp_url"] = serp_url
    q["serp_sha256"] = serp_sha256
    q["fetched_at"] = fetched_at
    q["detail"] = str(detail or "")[:200]
    if ok:
        q["status"] = "used"
        q["failure"] = None
        task["result_count"] = int(task.get("result_count", 0)) + len(ids)
        task["verification"] = "pending_verification"
    else:
        q["status"] = "failed"
        q["failure"] = failure_kind
        r["stats"]["query_failures"] = r["stats"].get("query_failures", 0) + 1
    r["stats"]["queries_executed"] = r["stats"].get("queries_executed", 0) + 1
    recent = r.setdefault("recent_queries", [])
    recent.append({
        "query_id": qid,
        "task_id": task["id"],
        "text": q.get("text"),
        "normalized": q.get("normalized") or normalize_query(q.get("text")),
        "round": st.get("rounds", 0),
        "outcome": q["status"],
        "result_count": len(ids),
        "failure": q.get("failure"),
    })
    del recent[:-config.RESEARCH_QUERY_HISTORY]
    task.setdefault("attempts", []).append({
        "round": st.get("rounds", 0),
        "query_id": qid,
        "purpose": q.get("purpose"),
        "outcome": q["status"],
        "result_count": len(ids),
        "failure": q.get("failure"),
    })
    task["updated_round"] = st.get("rounds", 0)
    status = None
    if len(task.get("attempts") or []) >= config.RESEARCH_ATTEMPTS_PER_TASK:
        status = _finalize(st, task)
    else:
        task["status"] = "active"
        if not ok:
            if failure_kind == "no_results":
                _plan_rewrite(st, r, task, q, failure_kind)
            else:
                _log(st, "query_strategy_switch", task_id=task["id"],
                     query_id=qid, failure=failure_kind,
                     strategy="下一次改用不同目的查询，不重复同一文本")
    _log(st, "query_result", query_id=qid, task_id=task["id"],
         purpose=q.get("purpose"), text=q.get("text"), ok=ok,
         result_count=len(ids), failure=q.get("failure"),
         task_status=status or task.get("status"),
         serp_url=serp_url, serp_sha256=serp_sha256)
    return q


def archived_pending(st, limit=20):
    """只读：被容量归档的待验证任务；含查询/来源快照，Phase 3 可继续消费。"""
    r = st.get("research")
    if not isinstance(r, dict) or not isinstance(r.get("archived_tasks"), list):
        return []
    items = [t for t in r["archived_tasks"]
             if isinstance(t, dict) and _has_materials(t)]
    items.sort(key=lambda t: -_int(t.get("updated_round"), 0))
    return items[:limit]


def focus(st, limit=3):
    """只读：返回当前最该关注的缺口（含候选材料待验证状态）。"""
    r = st.get("research")
    if not isinstance(r, dict) or not isinstance(r.get("tasks"), dict):
        return []
    rank = {"active": 0, "open": 1, "materials_found": 2}
    items = [t for t in r["tasks"].values()
             if t.get("status") not in CLOSED]
    items.sort(key=lambda t: (rank.get(t.get("status"), 3),
                              -t.get("priority", 0.0),
                              t.get("created_round", 0)))
    out = []
    for task in items[:limit]:
        out.append({
            "id": task.get("id"),
            "kind": task.get("kind"),
            "question": task.get("question"),
            "reason": task.get("reason"),
            "priority": task.get("priority"),
            "status": task.get("status"),
            "verification": task.get("verification"),
            "result_count": task.get("result_count", 0),
            "archived": False,
        })
    for task in archived_pending(st, limit=limit):
        if len(out) >= limit:
            break
        out.append({
            "id": task.get("id"),
            "kind": task.get("kind"),
            "question": task.get("question"),
            "reason": task.get("archive_reason") or "容量归档（待验证材料保留）",
            "priority": task.get("priority"),
            "status": task.get("status"),
            "verification": task.get("verification"),
            "result_count": task.get("result_count", 0),
            "archived": True,
        })
    return out


def summary(st):
    r = ensure_state(st)
    counts = Counter(t.get("status") for t in r["tasks"].values())
    return {
        "tasks": len(r["tasks"]),
        "status_counts": dict(counts),
        "queries": len(r["queries"]),
        "sources": len(r["sources"]),
        "archived_tasks": len(r.get("archived_tasks") or []),
        "miss_backlog": len(r.get("miss_backlog") or {}),
        "schema_version": r.get("schema_version"),
        "config_sha256": r.get("config_sha256"),
        "stats": dict(r.get("stats") or {}),
        "focus": focus(st),
    }
