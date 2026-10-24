# -*- coding: utf-8 -*-
"""abstraction — v9.18 二层：分层图（抽象节点自发生长）。

理论：CLS 互补学习系统（McClelland 1995）——底层是海马式的具体共现图
（现在的 concepts/relations），上层是新皮层式的抽象节点；巩固期（慢速）
从高权重共现簇里把抽象节点"长"出来，成员 ≥ABSTRACT_MIN_MEMBERS 才成类。
命名取簇内总连接度最高的成员（代表元，可与底层概念同名）；老师可在
FEED/回答里给它点正式名字（走既有 harvest/promote 通道，不另开协议）。
跨层激活由 activation.spread 的 siblings_fn/abstract_of_fn 钩子接入。"""
from . import config
from .config import (ABSTRACT_EDGE_W, ABSTRACT_MAX, ABSTRACT_MIN_MEMBERS,
                     ABSTRACT_PER_CONSOLIDATE)
from .state import append_note


def _union_find(edges):
    """edges: [(a,b)] → parent 映射。返回 {元素: 根}。"""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    return {x: find(x) for x in parent}


def grow_abstract(st):
    """巩固期调用：从强边 + confirmed 规则的连通分量里长抽象节点。返回新增数。"""
    cons = st.get("concepts", {})
    rels = st.get("relations", {})
    if len(cons) < ABSTRACT_MIN_MEMBERS:
        return 0
    edges = []
    for k, v in rels.items():
        try:
            a, b = k.split("|")
        except ValueError:
            continue
        if (v.get("w", 0) or 0) >= ABSTRACT_EDGE_W:
            edges.append((a, b))
    for v in st.get("rules", {}).values():
        if v.get("status") == "confirmed_stat":
            concl = v.get("conclusion", "")
            for p in v.get("premises", []):
                if p and concl and p != concl:
                    edges.append((p, concl))
    if not edges:
        return 0
    root = _union_find(edges)
    clusters = {}
    for member, r in root.items():
        clusters.setdefault(r, set())
        clusters[r].add(member)
    existing = st.setdefault("abstract", {})
    if len(existing) >= ABSTRACT_MAX:
        return 0
    # 总连接度（成员在 relations 所有含其边的 w 之和）→ 代表元命名
    degree = {}
    for k, v in rels.items():
        try:
            a, b = k.split("|")
        except ValueError:
            continue
        degree[a] = degree.get(a, 0) + (v.get("w", 0) or 0)
        degree[b] = degree.get(b, 0) + (v.get("w", 0) or 0)
    made = 0
    from .tokenizer import FORMAT_NOISE
    for members in sorted(clusters.values(), key=len, reverse=True):
        if made >= ABSTRACT_PER_CONSOLIDATE or len(existing) >= ABSTRACT_MAX:
            break
        # 成员须是正式概念且非格式噪声（摘要/来源这类排版词不配当"一类东西"）；
        # 巨型连通分量（遗留超级边把几十个概念吸成一坨）不成类
        members = {m for m in members if m in cons and m not in FORMAT_NOISE}
        if not (config.ABSTRACT_MIN_MEMBERS <= len(members) <= config.ABSTRACT_MAX_MEMBERS):
            continue
        # 防重复聚类：与任一现有抽象节点重叠过半 → 跳过
        dup = False
        for node in existing.values():
            inter = members & set(node.get("members", []))
            if inter and len(inter) / len(members) >= 0.5:
                dup = True
                break
        if dup:
            continue
        # 命名：按度数降序取第一个"干净"成员（不在噪声表、也不含噪声子串——
        # "又称概要是噪"这类批评句残渣不配当一类东西的名字）
        def _clean_name(m):
            if m in FORMAT_NOISE:
                return False
            return not any(nz in m for nz in FORMAT_NOISE)
        name = None
        for m in sorted(members, key=lambda x: -degree.get(x, 0)):
            if _clean_name(m):
                name = m
                break
        if name is None or name in existing:
            continue
        existing[name] = {"members": sorted(members), "born": st["rounds"],
                          "support": len(members), "ground": 0}
        append_note(st, f"- 抽象节点生长：『{name}』把 {'、'.join(sorted(members)[:5])} 打包成了一类（第 {st['rounds']} 轮）")
        made += 1
    return made


def bump_ground(st, concept, by=1):
    """v9.19 类比接地：concept 所属抽象节点的"验证经历"+1——
    只有踩过/预测过/对过的类，才准参与跨层类比（标签到标签的空桥不许搭）。"""
    name = abstract_of(st, concept)
    if name and name in st.get("abstract", {}):
        st["abstract"][name]["ground"] = st["abstract"][name].get("ground", 0) + by
    return name


def audit_abstract(st):
    """v9.19 聚类清理：踢掉与类内其他成员一条边都没有的残词；
    踢完成员 <ABSTRACT_MIN_MEMBERS 的类整队解散。类不是越大越好，也不能只进不出。"""
    rels = st.get("relations", {})
    removed_members = 0
    dead_nodes = []
    for name, node in st.get("abstract", {}).items():
        members = [m for m in node.get("members", []) if m in st.get("concepts", {})]
        kept = []
        for m in members:
            linked = False
            for other in members:
                if other == m:
                    continue
                k = "|".join(sorted([m, other]))
                if rels.get(k, {}).get("w", 0) >= 1:
                    linked = True
                    break
            if linked:
                kept.append(m)
            else:
                removed_members += 1
        if len(kept) < config.ABSTRACT_MIN_MEMBERS:
            dead_nodes.append(name)
        elif len(kept) != len(members):
            node["members"] = kept
            node["support"] = len(kept)
    for name in dead_nodes:
        node = st["abstract"].pop(name)
        append_note(st, f"- 抽象节点解散：『{name}』清走残词后有边的成员不足 {config.ABSTRACT_MIN_MEMBERS} 个，不成类，解散（原成员 {len(node.get('members', []))} 个）")
    return removed_members, len(dead_nodes)


def grounded_nodes(st, min_ground=None):
    """通过接地门槛（验证经历 ≥ 阈值）的抽象节点名。"""
    min_ground = config.ABSTRACT_GROUND_MIN if min_ground is None else min_ground
    return [n for n, node in st.get("abstract", {}).items()
            if node.get("ground", 0) >= min_ground
            and len(node.get("members", [])) >= config.ABSTRACT_MIN_MEMBERS]


def abstract_of(st, concept):
    """概念所属的抽象节点名（第一个命中）或 None。"""
    for name, node in st.get("abstract", {}).items():
        if concept in node.get("members", []):
            return name
    return None


def siblings_via_abstract(st, concept):
    """经抽象节点的跨层兄弟（同簇其他成员）。"""
    for node in st.get("abstract", {}).values():
        members = node.get("members", [])
        if concept in members:
            return [m for m in members if m != concept]
    return []


def abstract_nodes(st):
    """全部抽象节点名，按 support 降序。"""
    items = st.get("abstract", {}).items()
    return [n for n, _ in sorted(items, key=lambda kv: -kv[1].get("support", 0))]
