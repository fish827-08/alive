# -*- coding: utf-8 -*-
"""semantics.py — 语义读取模块（纯 Python，零依赖）

给 agent 一个"理解"的最小近似（没有 LLM 时的最接近方案）：
  1. text_to_vec：文本 → 512 维稀疏词频向量（hashing trick，log 词频，L2 归一化）
  2. AutoEncoder：512→32→512 单隐层自编码器，在线无监督训练
     （自监督：输入=输出目标，不需要任何标签；读得越多，编码器越会抓文本结构）
  3. 语义记忆库：每篇读过的文本保存 32 维"语义指纹"（memory.jsonl）
  4. 联想/对比：新文本与记忆库算余弦相似度 → 找最像的旧文、算"新奇度"
     —— 这就是"读懂了"的雏形：它知道自己读到的东西像什么、和什么不一样。

用法（被 agent_v6 调用）：
    sem = Semantics()                      # 加载/初始化
    vec = sem.text_to_vec(full_text)
    fp  = sem.encode(vec)                  # 32 维语义指纹
    sim = sem.cosine(fp, old_fp)
    top = sem.closest(fp)                  # (相似度, 记忆条目)
    sem.learn(vec, path, keywords)         # 训练一步 + 存入记忆库
"""
import json
import math
import random
import re
import time
from collections import Counter
from pathlib import Path

DIM = 512          # 词向量维度
HID = 32           # 语义指纹维度
LR = 0.03          # 学习率
TRAIN_STEPS = 20   # 每读一篇训练步数
MAX_CHARS = 8000   # 参与语义化的最大字符数


def _hash_pos(word):
    """确定性哈希：词 → [0, DIM) 的稀疏向量位置（一个词只占一个维度）。"""
    h = 0
    for ch in word.encode("utf-8"):
        h = (h * 131 + ch) & 0xFFFFFFFF
    return h % DIM


def tokenize(text):
    """分词：中文 2 字以上 + 英文 4 字符以上，统一小写。"""
    return re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{4,}", text.lower())


def text_to_vec(text):
    """文本 → L2 归一化的稀疏词频向量（log 词频压缩长尾）。"""
    text = text[:MAX_CHARS]
    words = tokenize(text)
    cnt = Counter(words)
    v = [0.0] * DIM
    for w, c in cnt.items():
        v[_hash_pos(w)] += math.log1p(c)
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class AutoEncoder:
    """单隐层自编码器 512→32→512（ReLU 隐层）。在线 SGD，自监督。"""

    def __init__(self, dim=DIM, hid=HID, lr=LR):
        self.dim, self.hid, self.lr = dim, hid, lr
        self.W1 = [[0.10 * (random.random() - 0.5) for _ in range(hid)] for _ in range(dim)]
        self.b1 = [0.0] * hid
        self.W2 = [[0.10 * (random.random() - 0.5) for _ in range(dim)] for _ in range(hid)]
        self.b2 = [0.0] * dim

    def encode(self, v):
        h = [self.b1[j] + sum(v[i] * self.W1[i][j] for i in range(self.dim)) for j in range(self.hid)]
        return [x if x > 0.0 else 0.0 for x in h]

    def decode(self, h):
        out = [self.b2[i] + sum(h[j] * self.W2[j][i] for j in range(self.hid)) for i in range(self.dim)]
        return [x if x > 0.0 else 0.0 for x in out]

    def train(self, v, steps=TRAIN_STEPS):
        """一步在线自监督训练：重建误差反向传播。"""
        for _ in range(steps):
            h = self.encode(v)
            rec = self.decode(h)
            # 输出层（W2, b2）
            for j in range(self.hid):
                hj = h[j]
                if hj == 0.0:
                    continue
                row = self.W2[j]
                for i in range(self.dim):
                    row[i] -= self.lr * (rec[i] - v[i]) * hj
            for i in range(self.dim):
                self.b2[i] -= self.lr * (rec[i] - v[i])
            # 隐层（W1, b1），ReLU 导数
            for j in range(self.hid):
                if h[j] <= 0.0:
                    continue
                grad_h = sum((rec[i] - v[i]) * self.W2[j][i] for i in range(self.dim))
                for i in range(self.dim):
                    self.W1[i][j] -= self.lr * grad_h * v[i]
                self.b1[j] -= self.lr * grad_h

    def to_json(self):
        return {"dim": self.dim, "hid": self.hid, "lr": self.lr,
                "W1": self.W1, "b1": self.b1, "W2": self.W2, "b2": self.b2}

    @classmethod
    def from_json(cls, d):
        ae = cls(d["dim"], d["hid"], d.get("lr", LR))
        ae.W1, ae.b1, ae.W2, ae.b2 = d["W1"], d["b1"], d["W2"], d["b2"]
        return ae


def cosine(a, b):
    """两个 32 维向量余弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class Semantics:
    """语义引擎：编码器 + 记忆库。"""

    def __init__(self, base):
        self.dir = Path(base) / "semantics"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ae_file = self.dir / "ae.json"
        self.mem_file = self.dir / "memory.jsonl"
        self.ae = self._load_ae()
        self.memory = self._load_memory()

    def _load_ae(self):
        try:
            return AutoEncoder.from_json(json.loads(self.ae_file.read_text(encoding="utf-8")))
        except Exception:
            return AutoEncoder()

    def _load_memory(self):
        mem = []
        if self.mem_file.exists():
            for line in self.mem_file.read_text(encoding="utf-8").splitlines():
                try:
                    mem.append(json.loads(line))
                except Exception:
                    continue
        return mem

    def warmup(self, texts, steps_each=5, max_items=800):
        """部署预热：用一批无标签文本训练编码器（自监督重建），
        让它先学会"把文本压缩成指纹而不丢信息"，之后指纹才有语义区分度。
        文本来源可以是外部扫描到的系统文档/口粮资料——不需要任何标签。"""
        vecs = [text_to_vec(t) for t in texts[:max_items] if t and t.strip()]
        if not vecs:
            return 0
        for v in vecs:
            self.ae.train(v, steps=steps_each)
        return len(vecs)

    def save(self):
        self.ae_file.write_text(json.dumps(self.ae.to_json()), encoding="utf-8")
        with open(self.mem_file, "a", encoding="utf-8") as f:
            for item in self.memory:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        # 追加后清内存，避免重复写
        self.memory = []

    def learn(self, vec, path, keywords, kind="text"):
        """训练编码器 + 把语义指纹存入记忆库。返回指纹。"""
        fp = self.ae.encode(vec)
        self.ae.train(vec)
        self.memory.append({
            "path": path, "vec": fp, "keywords": keywords,
            "kind": kind, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        return fp

    def closest(self, fp):
        """与记忆库最相似的条目。返回 (相似度, 条目) 或 (0.0, None)。"""
        best = (0.0, None)
        for item in self.memory:
            s = cosine(fp, item["vec"])
            if s > best[0]:
                best = (s, item)
        return best

    def avg_sim(self, fp, pool_paths=None):
        """fp 与（部分）记忆的平均相似度，衡量"见过的程度"= 新奇度的反面。"""
        items = self.memory
        if pool_paths is not None:
            items = [m for m in self.memory if m["path"] in pool_paths]
        if not items:
            return 0.0
        return sum(cosine(fp, m["vec"]) for m in items) / len(items)
