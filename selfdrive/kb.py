# -*- coding: utf-8 -*-
"""kb — rag-kb 记忆服务客户端（v7 引入，v9.16 拆出；kb-memory 2.1.2 @ 127.0.0.1:8100）。"""
import json
import urllib.request

KB_URL = "http://127.0.0.1:8100"
KB_TIMEOUT = 20


def kb_remember(content, tags, source):
    """全文入记忆库（真向量嵌入 + BM25）。失败静默返回 False。"""
    try:
        body = json.dumps({
            "content": content, "tags": tags, "source": source,
            "agent_id": "selfdrive", "client": "selfdrive-agent",
            "namespace": "journey",
        }).encode("utf-8")
        req = urllib.request.Request(
            KB_URL + "/api/v1/memories", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=KB_TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False


def kb_recall(query, top_k=3):
    """混合检索相关记忆。失败/空 → []。"""
    try:
        body = json.dumps({"query": query, "top_k": top_k}).encode("utf-8")
        req = urllib.request.Request(
            KB_URL + "/api/v1/search", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=KB_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("results", [])
    except Exception:
        return []
