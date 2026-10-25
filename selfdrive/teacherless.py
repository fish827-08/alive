# -*- coding: utf-8 -*-
"""teacherless — 无教师实验的隔离基线、种子来源账本与执行层硬门禁。

阶段 1 只证明一件事：系统可以在不访问教师和 LLM 的条件下运行，且没有把
教师历史当成自己的新知识。研究任务队列与定向搜索在阶段 2 由 research.py
实现，但搜索结果只是未验证候选材料；统一验证器和独立评估仍属于后续阶段，
不在本模块里假装已经实现。

关键约束：
  - 必须使用独立工作目录与独立 state/logs；拒绝在含教师载荷的目录上初始化。
  - 教师/LLM/兄弟信箱/外部目录扫描/共享向量库全部硬禁用，不靠权重降低概率。
  - 初始化时写 manifest，逐项记录种子来源；from_zero 只允许代码级先验，
    不允许任何教师历史、网络资料或旧笔记被静默带入。
  - 运行时只检查“基线之后有没有新增教师/LLM 痕迹”；不会把历史载荷洗白。
"""
import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

from . import VERSION, config

MANIFEST_NAME = "teacherless_manifest.json"
AUDIT_NAME = "teacherless_audit.jsonl"
FORMAT = "alive-teacherless-manifest/1"

BLOCKED_SKILLS = frozenset({
    "ask_brain", "read_brain", "llm_consult", "sibling_letter", "explore_wider",
})
SEED_SOURCES = frozenset({
    "base_code", "training_material", "teacher_history", "web", "inference", "unknown",
})
TEACHER_ARTIFACT_KEYS = (
    "teacher_questions", "teacher_answers_read", "teacher_rules_compiled",
    "teacher_verdicts", "llm_proposals", "llm_calls",
    "sibling_letters_read", "wider_files",
)


def is_enabled():
    return bool(getattr(config, "TEACHERLESS", False))


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _base(base=None):
    return Path(base or config.BASE).expanduser().resolve()


def _canonical_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _hash_file(path):
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _hash_path(path):
    """文件/目录内容哈希；目录按相对路径+文件哈希聚合，拒绝符号链接。"""
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f"种子路径不允许符号链接：{path}")
    if path.is_file():
        sha, size = _hash_file(path)
        return sha, size, 1
    if not path.is_dir():
        raise ValueError(f"种子路径既不是文件也不是目录：{path}")
    digest = hashlib.sha256()
    total = 0
    count = 0
    for p in sorted(x for x in path.rglob("*") if x.is_file()):
        if p.is_symlink():
            raise ValueError(f"种子目录内不允许符号链接：{p}")
        rel = str(p.relative_to(path)).replace("\\", "/")
        sha, size = _hash_file(p)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha.encode("ascii"))
        digest.update(b"\0")
        total += size
        count += 1
    if count == 0:
        raise ValueError(f"种子目录为空：{path}")
    return digest.hexdigest(), total, count


def _code_fingerprint():
    """对实际运行的代码取指纹，避免把“从零”建立在未记录的代码先验上。"""
    root = config.BASE
    files = [root / "semantics.py", root / "agent_v9.py"]
    pkg = root / "selfdrive"
    if pkg.exists():
        files.extend(sorted(p for p in pkg.glob("*.py") if p.is_file()))
    digest = hashlib.sha256()
    used = []
    for path in files:
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        used.append(rel)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest(), used


def code_fingerprint():
    """当前运行代码的指纹（用于判断实验启动后代码先验是否发生漂移）。"""
    return _code_fingerprint()[0]


def _payloads(base):
    """初始化前检查知识载荷；代码目录不算，数据/教师通道/旧日志都算。"""
    found = []
    if (base / "state.json").exists():
        found.append("state.json")
    for rel in ("brain", "workspace", "logs", "semantics"):
        root = base / rel
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != ".gitkeep":
                found.append(str(path.relative_to(base)).replace("\\", "/"))
    return found


def _manifest_path(base):
    return base / MANIFEST_NAME


def load_manifest(base=None):
    path = _manifest_path(_base(base))
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Teacherless manifest 损坏，拒绝静默重建：{path}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise RuntimeError(f"Teacherless manifest 格式不合法：{path}")
    items = data.get("seed_items")
    if not isinstance(items, list) or not items:
        raise RuntimeError(f"Teacherless manifest 缺少种子清单：{path}")
    if data.get("seed_items_sha256") != _canonical_hash(items):
        raise RuntimeError(
            f"Teacherless manifest 种子清单哈希不匹配，拒绝加载：{path}")
    return data


def _require_code_version(manifest):
    """实验期间代码版本必须固定；升级只能走显式迁移记录。"""
    if not manifest or not manifest.get("code_sha256"):
        raise RuntimeError("Teacherless manifest 缺少 code_sha256")
    current = code_fingerprint()
    expected = manifest["code_sha256"]
    if current != expected:
        raise RuntimeError(
            "拒绝继续：代码指纹已漂移，实验期间代码版本必须固定。"
            f"manifest={expected[:12]} current={current[:12]}。"
            "如确认升级，先运行 py -m selfdrive.teacherless accept-code "
            "--reason \"<升级原因>\" 写入迁移记录；重大算法变更应新建实验。")
    return current


def seed_problems(manifest, base=None):
    """复核外部种子文件仍在、内容哈希/大小未变；代码先验不参与。"""
    if not manifest:
        return ["manifest 不存在"]
    base = _base(base)
    problems = []
    for index, item in enumerate(manifest.get("seed_items") or []):
        if not isinstance(item, dict):
            problems.append(f"seed_items[{index}] 不是对象")
            continue
        if item.get("source_type") == "base_code":
            continue
        stored = item.get("stored_at")
        if stored:
            if not isinstance(stored, str):
                problems.append(f"seed_items[{index}] stored_at 不是字符串")
                continue
            path = (base / stored).resolve()
            if base not in path.parents and path != base:
                problems.append(f"seed_items[{index}] stored_at 越出实验目录：{stored}")
                continue
        else:
            raw = item.get("path")
            if not isinstance(raw, str) or not raw.strip():
                problems.append(f"seed_items[{index}] 缺少可复核 path")
                continue
            path = Path(raw)
        if not path.exists():
            problems.append(f"seed_items[{index}] 种子文件缺失：{path}")
            continue
        try:
            sha, size, count = _hash_path(path)
        except (OSError, ValueError) as exc:
            problems.append(f"seed_items[{index}] 种子无法复核：{exc}")
            continue
        if sha != item.get("sha256"):
            problems.append(f"seed_items[{index}] 内容哈希已变化：{path}")
        if size != item.get("bytes"):
            problems.append(f"seed_items[{index}] 大小已变化：{path}")
        if count != item.get("file_count"):
            problems.append(f"seed_items[{index}] 文件数已变化：{path}")
    return problems


def accept_code_change(reason, base=None):
    """显式接受代码升级：保留旧指纹与原因，禁止静默覆盖。"""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("代码迁移必须提供 reason")
    base = _base(base)
    manifest = load_manifest(base)
    if manifest is None:
        raise RuntimeError("Teacherless manifest 不存在")
    previous = manifest.get("code_sha256")
    if not previous:
        raise RuntimeError("manifest 缺少 code_sha256，无法迁移")
    current = code_fingerprint()
    if current == previous:
        raise RuntimeError("代码指纹未变化，无需迁移")
    migration = {
        "at": _now(),
        "from_sha256": previous,
        "to_sha256": current,
        "from_version": manifest.get("code_version"),
        "to_version": VERSION,
        "reason": reason[:300],
    }
    manifest.setdefault("code_sha256_initial", previous)
    manifest.setdefault("code_migrations", []).append(migration)
    manifest["code_sha256"] = current
    manifest["code_version"] = VERSION
    _atomic_write(_manifest_path(base),
                  json.dumps(manifest, ensure_ascii=False, indent=1))
    audit(base, "code_migration", "runtime",
          f"{previous[:12]}→{current[:12]} reason={reason[:80]}")
    return migration


def _seed_policy_note(policy):
    if policy == "from_zero":
        return ("未继承历史运行状态或预装经验语料；但仍包含代码级先验"
                "（内置模板、加法/能量概念卡、L001 假设、技能权重、情绪默认值），"
                "这些先验在 seed_items 中登记，不计为实验中学到的证据。")
    if policy == "teacher_history_seeded":
        return ("显式带入教师历史数据，只能作为“停止教学后的自主延续”对照，"
                "不能宣称完全从零或未受教师影响。")
    return "显式带入外部初始经验数据，不是完全从零；来源、哈希与大小见 seed_items。"


def _describe_external_seed(entry, base, index, created):
    """外部种子必须给真实路径；记录路径/哈希/大小/导入时间，可选复制进 archive。"""
    source = entry.get("source_type")
    if source not in SEED_SOURCES:
        raise ValueError(f"未知种子来源：{source}")
    if source == "base_code":
        raise ValueError("外部种子不能声明 base_code（代码先验由系统自动登记）")
    raw = str(entry.get("path") or "").strip()
    if not raw:
        raise ValueError(f"种子 {source} 必须提供真实 path；只写描述不算可核验的来源")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"种子路径不存在：{path}")
    sha, size, count = _hash_path(path)
    item = {
        "source_type": source,
        "kind": entry.get("kind", "corpus"),
        "path": str(path),
        "sha256": sha,
        "bytes": size,
        "file_count": count,
        "imported_at": _now(),
        "imported": bool(entry.get("import")),
    }
    for key in ("description", "url", "note"):
        if entry.get(key):
            item[key] = entry[key]
    if item["imported"]:
        target = base / "workspace" / "archive" / f"seed-{index:02d}-{path.name}"
        if target.exists():
            raise RuntimeError(f"种子导入目标已存在：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        created.append(target)
        try:
            if path.is_dir():
                shutil.copytree(path, target)
            else:
                shutil.copy2(path, target)
            stored = _hash_path(target)
            if stored != (sha, size, count):
                raise RuntimeError(f"种子导入后校验不一致：{path}")
        except Exception:
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            except OSError:
                pass
            created.remove(target)
            raise
        item["stored_at"] = str(target.relative_to(base)).replace("\\", "/")
    return item


def _seed_items(seed_entries=None, base=None, created=None):
    fingerprint, used = _code_fingerprint()
    items = [{
        "source_type": "base_code",
        "kind": "runtime_code",
        "paths": used,
        "sha256": fingerprint,
    }, {
        "source_type": "base_code",
        "kind": "builtin_priors",
        "description": ("内置提问模板、加法/能量概念卡、L001 假设、技能权重与情绪默认值；"
                        "这些是代码先验，不是本实验从数据中学到的知识"),
    }]
    base = _base(base)
    created = created if created is not None else []
    for index, entry in enumerate(seed_entries or [], start=1):
        if not isinstance(entry, dict):
            raise ValueError("seed_entries 每项必须是对象")
        items.append(_describe_external_seed(entry, base, index, created))
    return items


def _seed_policy(seed_entries):
    sources = {e.get("source_type") for e in seed_entries or []}
    if "teacher_history" in sources:
        return "teacher_history_seeded"
    if sources - {"base_code"}:
        return "declared"
    return "from_zero"


def init_experiment(base=None, experiment_id=None, agent_name=None, seed_entries=None):
    """在独立目录建立 Teacherless 基线。返回 manifest。

    默认 from_zero：目录里不得已有 state/brain/workspace/logs/semantics 载荷。
    若显式声明外部种子，manifest 会记录来源；teacher_history 会被标成
    teacher_history_seeded，不能再宣称“完全从零”。
    """
    base = _base(base)
    if _manifest_path(base).exists():
        raise RuntimeError(f"该目录已经初始化过 Teacherless：{_manifest_path(base)}")
    payloads = _payloads(base)
    if payloads:
        preview = "、".join(payloads[:8])
        raise RuntimeError(
            f"拒绝初始化：目录已有知识载荷（{preview}）。"
            "Teacherless 必须使用干净目录；关闭教师通信不等于从零开始。")

    experiment_id = (experiment_id or time.strftime("TL-%Y%m%d-%H%M%S")).strip()
    if not experiment_id:
        raise ValueError("experiment_id 不能为空")
    seed_entries = list(seed_entries or [])
    for rel in ("logs", "brain/templates", "workspace/archive",
                "workspace/notes", "workspace/web", "semantics"):
        (base / rel).mkdir(parents=True, exist_ok=True)

    copied = []
    try:
        items = _seed_items(seed_entries, base=base, created=copied)
        policy = _seed_policy(seed_entries)
        fingerprint, _used = _code_fingerprint()
        manifest = {
            "format": FORMAT,
            "experiment_id": experiment_id,
            "mode": "teacherless",
            "created_at": _now(),
            "base_dir": str(base),
            "code_version": VERSION,
            "code_sha256": fingerprint,
            "code_sha256_initial": fingerprint,
            "code_migrations": [],
            "seed_policy": policy,
            "seed_policy_note": _seed_policy_note(policy),
            "clean_from_zero": policy == "from_zero",
            "seed_items": items,
            "seed_items_sha256": _canonical_hash(items),
            "channels_disabled": sorted(BLOCKED_SKILLS) + ["shared_kb"],
            "llm_enabled": False,
            "web_search": "observation_allowed",
            "notes": ("阶段 1.1 隔离基线：只验证教师/LLM 零调用、独立状态、"
                      "代码版本固定、种子来源可核验；阶段 2 已加入研究任务队列与"
                      "定向搜索（结果仅为未验证候选材料），"
                      "统一验证器与评估尚未实现。"),
        }
        _atomic_write(_manifest_path(base),
                      json.dumps(manifest, ensure_ascii=False, indent=1))
    except Exception:
        for target in reversed(copied):
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            except OSError:
                pass
        raise
    if agent_name:
        (base / "agent_name.txt").write_text(agent_name.strip(), encoding="utf-8")
    audit(base, "init", "runtime",
          f"experiment={experiment_id} seed_policy={policy} code={fingerprint[:12]}")
    return manifest


def contamination_markers(base=None):
    """扫描初始化后可能被教师/LLM/兄弟信箱污染的落点。"""
    base = _base(base)
    marks = {}
    notes = base / "workspace" / "notes" / "journey.md"
    if notes.exists():
        text = notes.read_text(encoding="utf-8", errors="ignore")
        for key, marker in (
            ("teacher_answer_notes", "## 大脑回答"),
            ("teacher_verdict_notes", "教师判定"),
            ("llm_proposal_notes", "LLM提案"),
            ("sibling_letter_notes", "兄弟来信"),
        ):
            count = text.count(marker)
            if count:
                marks[key] = count
    inbox = base / "brain" / "inbox.md"
    if inbox.exists() and inbox.stat().st_size > 0:
        marks["teacher_inbox_present"] = 1
    outbox = base / "brain" / "outbox"
    if outbox.exists():
        count = sum(1 for p in outbox.glob("*.md"))
        if count:
            marks["teacher_outbox_present"] = count
    return marks


def log_channel_counts(base=None):
    """从事件日志反查被禁技能是否真的执行过（不是只看权重）。"""
    base = _base(base)
    path = base / "logs" / "events.jsonl"
    if not path.exists():
        return {}
    counts = Counter()
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            action = row.get("action")
            if action in BLOCKED_SKILLS:
                counts[action] += 1
    except Exception:
        return {}
    return dict(counts)


def artifact_counts(st):
    """统计状态里的教师/LLM/外部继承痕迹。"""
    llm_budget = st.get("llm_budget") or {}
    think_budget = st.get("think_budget") or {}
    return {
        "teacher_questions": len(st.get("brain_questions") or []),
        "teacher_answers_read": len(st.get("brain_read") or []),
        "teacher_rules_compiled": len(st.get("compiled_rules") or {}),
        "teacher_verdicts": sum(
            1 for item in st.get("infer_log") or []
            if isinstance(item, dict) and item.get("teacher_verdict")),
        "llm_proposals": len(st.get("llm_proposals") or []),
        "llm_calls": int(llm_budget.get("calls", 0)) + int(think_budget.get("calls", 0)),
        "sibling_letters_read": len(st.get("letters_read") or []),
        "wider_files": len(st.get("wider_files") or {}),
    }


def _load_state_file(base):
    path = base / "state.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Teacherless state.json 损坏：{path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Teacherless state.json 顶层必须是对象：{path}")
    return data


def _marker_violations(st, manifest):
    marker = st.get("teacherless")
    if not isinstance(marker, dict):
        return ["state.json 没有 teacherless 标记（疑似教师历史延续）"]
    problems = []
    if marker.get("experiment_id") != manifest.get("experiment_id"):
        problems.append("state 的 experiment_id 与 manifest 不一致")
    if marker.get("seed_items_sha256") != manifest.get("seed_items_sha256"):
        problems.append("state 的种子清单哈希与 manifest 不一致")
    return problems


def verify_clean_start(base=None):
    """启动前检查：已初始化、state 属于同一实验、无新增教师/LLM 痕迹。"""
    if not is_enabled():
        return None
    base = _base(base)
    manifest = load_manifest(base)
    if manifest is None:
        raise RuntimeError(
            f"Teacherless 未初始化：{_manifest_path(base)} 不存在。"
            "先在独立目录运行 py -m selfdrive.teacherless init")
    _require_code_version(manifest)
    problems = seed_problems(manifest, base)
    if problems:
        raise RuntimeError("拒绝启动：种子清单复核失败 " + "；".join(problems))
    st = _load_state_file(base)
    if st is not None:
        problems = _marker_violations(st, manifest)
        if problems:
            raise RuntimeError("拒绝启动：" + "；".join(problems))
        assert_state_clean(st, manifest, base=base)
    if manifest.get("seed_policy") == "from_zero":
        marks = contamination_markers(base)
        channels = log_channel_counts(base)
        if marks or channels:
            raise RuntimeError(
                "拒绝启动：from_zero 基线发现教师/LLM 痕迹 "
                f"notes={marks} logs={channels}")
    return manifest


def stamp_state(st, manifest=None):
    """把实验身份与基线计数写入 state；基线之后只允许不增加。"""
    if not is_enabled():
        return None
    manifest = manifest or load_manifest()
    if manifest is None:
        raise RuntimeError("Teacherless manifest 不存在")
    _require_code_version(manifest)
    existing = st.get("teacherless")
    if isinstance(existing, dict) and existing.get("experiment_id"):
        problems = _marker_violations(st, manifest)
        if problems:
            raise RuntimeError("拒绝复用其他实验的状态：" + "；".join(problems))
        return existing
    counts = artifact_counts(st)
    if manifest.get("seed_policy") == "from_zero":
        nonzero = {k: v for k, v in counts.items() if v}
        if nonzero:
            raise RuntimeError(f"拒绝初始化 state：from_zero 基线已有教学痕迹 {nonzero}")
    marker = {
        "experiment_id": manifest["experiment_id"],
        "seed_policy": manifest.get("seed_policy"),
        "seed_items_sha256": manifest.get("seed_items_sha256"),
        "baseline_counts": counts,
        "started_at": _now(),
    }
    st["teacherless"] = marker
    return marker


def assert_state_clean(st, manifest=None, base=None):
    """检查基线之后没有新增教师/LLM 调用或外部继承。"""
    if not is_enabled():
        return None
    base = _base(base)
    manifest = manifest or load_manifest(base)
    if manifest is None:
        raise RuntimeError("Teacherless manifest 不存在")
    _require_code_version(manifest)
    problems = _marker_violations(st, manifest)
    if problems:
        raise RuntimeError("Teacherless 状态不合法：" + "；".join(problems))
    marker = st["teacherless"]
    baseline = marker.get("baseline_counts") or {}
    current = artifact_counts(st)
    violations = {
        key: {"baseline": baseline.get(key, 0), "current": value}
        for key, value in current.items()
        if value > baseline.get(key, 0)
    }
    if violations:
        raise RuntimeError(f"Teacherless 基线被突破：{violations}")
    if manifest.get("seed_policy") == "from_zero":
        marks = contamination_markers(base)
        channels = log_channel_counts(base)
        if marks or channels:
            raise RuntimeError(
                "Teacherless from_zero 基线发现污染 "
                f"notes={marks} logs={channels}")
    return current


def assert_mode_matches_state(st):
    """standard 模式不许启动 Teacherless 状态（会重新打开教师/LLM 通道）。"""
    if is_enabled():
        return
    if st.get("teacherless"):
        raise RuntimeError(
            "该 state 属于 Teacherless 实验，禁止用 standard 模式启动"
            "（会重新打开教师/LLM 通道，破坏实验隔离）")


def allow_action(action):
    return not is_enabled() or action not in BLOCKED_SKILLS


def blocked_reason(action):
    return (f"Teacherless 硬禁用：{action} 属于教师/LLM/外部继承通道，"
            "不参与本实验的候选与执行")


def audit(base=None, event="", channel="", detail="", round=None):
    """追加一条审计记录；审计失败不阻断隔离本身。"""
    try:
        base = _base(base)
        logs = base / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        row = {"ts": _now(), "event": event, "channel": channel,
               "detail": detail[:300]}
        if round is not None:
            row["round"] = round
        with open(logs / AUDIT_NAME, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def status(base=None):
    base = _base(base)
    manifest = load_manifest(base)
    st = _load_state_file(base)
    result = {
        "base_dir": str(base),
        "mode_flag": is_enabled(),
        "initialized": manifest is not None,
        "manifest": manifest,
    }
    drift = None
    seeds = []
    if manifest is not None:
        current_code = code_fingerprint()
        drift = current_code != manifest.get("code_sha256")
        seeds = seed_problems(manifest, base)
        result.update({
            "code_sha256_current": current_code,
            "code_sha256_initial": manifest.get("code_sha256_initial",
                                               manifest.get("code_sha256")),
            "code_drift": drift,
            "code_migrations": manifest.get("code_migrations") or [],
            "seed_problems": seeds,
        })
    if st is None:
        result.update({"state_exists": False, "artifact_counts": None,
                       "baseline_counts": None,
                       "clean": manifest is not None and not drift and not seeds})
        return result
    marker = st.get("teacherless") or {}
    current = artifact_counts(st)
    baseline = marker.get("baseline_counts") or {}
    violations = {k: v for k, v in current.items() if v > baseline.get(k, 0)}
    marker_problems = _marker_violations(st, manifest) if manifest else ["manifest 不存在"]
    marks = contamination_markers(base)
    channels = log_channel_counts(base)
    result.update({
        "state_exists": True,
        "state_rounds": st.get("rounds", 0),
        "artifact_counts": current,
        "baseline_counts": baseline,
        "violations": violations,
        "marker_problems": marker_problems,
        "contamination_markers": marks,
        "blocked_channel_log_counts": channels,
        "clean": (not violations and not marker_problems and not marks
                  and not channels and not drift and not seeds),
    })
    return result


def _main():
    parser = argparse.ArgumentParser(description="Alive Teacherless 基线工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="初始化独立 Teacherless 工作目录")
    p_init.add_argument("--base", default=None)
    p_init.add_argument("--experiment", default=None)
    p_init.add_argument("--name", default=None)
    p_status = sub.add_parser("status", help="显示基线计数与污染检查")
    p_status.add_argument("--base", default=None)
    p_accept = sub.add_parser("accept-code", help="显式接受代码升级并记录迁移")
    p_accept.add_argument("--base", default=None)
    p_accept.add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.cmd == "init":
        manifest = init_experiment(args.base, args.experiment, args.name)
        print(json.dumps(manifest, ensure_ascii=False, indent=1))
    elif args.cmd == "accept-code":
        migration = accept_code_change(args.reason, args.base)
        print(json.dumps(migration, ensure_ascii=False, indent=1))
    else:
        print(json.dumps(status(args.base), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    _main()
