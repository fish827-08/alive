# -*- coding: utf-8 -*-
"""self_mod — v9.23 自我修改的安全写入器（外围代码自改能力）。

设计原则（四权分立的延伸）：
  LLM/自省只有**修改提案权**；写入必须经过本模块的白名单校验、参数夹紧、
  自动备份三道门——**权限在代码里，不在 LLM 的自觉里**。
  即使 LLM 幻觉出"删除核心文件"的提案，写入器直接拒绝。

外围/核心边界（核心 = 改了会死机或失忆，一律拒绝）：
  核心禁区：selfdrive/、agent_v9.py、semantics.py、state.json、agent_name.txt、
            requirements.txt、../（目录逃逸）、绝对路径
  外围白名单（改错只导致该功能回退默认——所有读取端都有 try/except）：
    brain/emotions.json   情感参数（夹紧：decay/gain 有界）
    brain/ask_config.json 决策参数（白名单键+值域）
    brain/templates/*.txt 提问模板（损坏会被 load_templates 跳过）
    brain/PROTOCOL.md     自我说明书
    workspace/**          他们自己的世界（本来就自由）

每次成功写入：先写 .bak 备份 + 在修改史 self_mod_log 记录（谁、改了什么、何时）。
"""
import json
import re
import shutil

from . import config
from .state import append_note

# 白名单（fnmatch 风格，相对 BASE）
ALLOWED = [
    "brain/emotions.json",
    "brain/ask_config.json",
    "brain/templates/*.txt",
    "brain/PROTOCOL.md",
    "workspace/**",
]
# 核心禁区特征（任何路径命中即拒绝，双保险）
FORBIDDEN_PATTERNS = [
    r"selfdrive", r"agent_v9", r"semantics", r"state\.json",
    r"agent_name", r"requirements", r"\.\./", r"^[A-Za-z]:\\", r"^/",
]
# ask_config 允许改的键与值域（防自杀式调参）
ASK_CONFIG_LIMITS = {
    "open_ask.cooldown_rounds": (0, 100),
    "open_ask.random_prob": (0.0, 1.0),
    "web_search.cooldown_rounds": (5, 200),
    "urge.linear_factor": (0.0, 0.6),
}
# emotions 允许改的键与值域（夹紧，防情绪自杀）
EMOTIONS_LIMITS = {
    "decay": (0.0, 0.15),
    "novelty_gain": (0.0, 0.3),
    "success_gain": (0.0, 0.5),
}


def check_path(relpath):
    """路径白名单校验。返回 (ok, 原因)。"""
    p = (relpath or "").replace("\\", "/").lstrip("./")
    for pat in FORBIDDEN_PATTERNS:
        if re.search(pat, p):
            return False, f"核心禁区或路径逃逸（命中 {pat}）"
    import fnmatch
    for allowed in ALLOWED:
        if fnmatch.fnmatch(p, allowed) or (allowed.endswith("/**") and p.startswith(allowed[:-2])):
            return True, "白名单内"
    return False, "不在白名单（只允许：emotions/ask_config/templates/PROTOCOL/workspace）"


def _clamp_emotion_value(key, value):
    if key in EMOTIONS_LIMITS:
        lo, hi = EMOTIONS_LIMITS[key]
        return max(lo, min(hi, float(value)))
    return None


def write_external(st, relpath, content, who):
    """唯一的外围写入入口：白名单校验 → 备份 → 写入 → 修改史。
    返回 (ok, 原因)。任何异常都吞掉并拒绝——写入器本身不能成为死机源。"""
    ok, why = check_path(relpath)
    if not ok:
        append_note(st, f"- 自改被拒：{relpath}（{why}）——核心不可触碰")
        return False, why
    try:
        target = config.BASE / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():   # 备份（只留一代，防备份膨胀）
            shutil.copy2(target, str(target) + ".selfmod.bak")
        if isinstance(content, str):
            target.write_text(content, encoding="utf-8")
        else:
            target.write_text(json.dumps(content, ensure_ascii=False, indent=1), encoding="utf-8")
        log = st.setdefault("self_mod_log", [])
        log.append({"round": st["rounds"], "who": who, "path": relpath,
                    "size": len(str(content))})
        del log[:-50]
        append_note(st, f"- 自我修改（{who}）：{relpath} 已更新（已备份，下一轮生效）")
        return True, "已写入并备份"
    except Exception as exc:
        append_note(st, f"- 自改失败：{relpath}（{exc.__class__.__name__}）——系统不受影响")
        return False, str(exc)


def clamp_emotions_config(cfg):
    """情感参数夹紧：越界的值拉回界内（防把衰减调成 0 自嗨、把增益调成 1 自杀）。"""
    changed = 0
    for dim, rule in cfg.items():
        if not isinstance(rule, dict):
            continue
        for key, (lo, hi) in EMOTIONS_LIMITS.items():
            if key in rule:
                try:
                    v = float(rule[key])
                except (TypeError, ValueError):
                    continue
                clamped = max(lo, min(hi, v))
                if clamped != v:
                    rule[key] = clamped
                    changed += 1
    return changed


def validate_ask_config(cfg):
    """ask_config 校验：未知键剔除、越界值夹紧。返回 (clean_cfg, 剔除数, 夹紧数)。"""
    clean = {}
    removed = clamped = 0
    for full_key, (lo, hi) in ASK_CONFIG_LIMITS.items():
        section, _, key = full_key.partition(".")
        try:
            v = float(cfg.get(section, {}).get(key))
        except (TypeError, ValueError):
            continue
        clean.setdefault(section, {})[key] = max(lo, min(hi, v))
        if v != clean[section][key]:
            clamped += 1
    return clean, removed, clamped
