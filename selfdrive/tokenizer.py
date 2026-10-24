# -*- coding: utf-8 -*-
"""tokenizer — 分词器官（v9.16 ⑤）。

诊断结论：碎片率 21% 的病根不是窗口大小，是边界——旧代码
re.findall(r"[\\u4e00-\\u9fa5]{2,6}") 的切分边界是标点和第 6 个字，不是词界。
方案：jieba 优先（装了就用）；没装自动回退旧滑窗 + 子串合并（保证可运行）。
这不是"喂词表"，是配器官——词汇本身仍要靠 learning 的晋升制挣来。
"""
import json
import re

try:
    import jieba as _jieba
    try:   # 关闭 jieba 初始化日志（老版本 API 名不同，双双兜底）
        _jieba.setLogLevel(60)
    except Exception:
        pass
    HAS_JIEBA = True
except Exception:
    _jieba = None
    HAS_JIEBA = False

from .config import NOISY_WORDS_FILE

# 格式噪声词（网页排版/对话套话，非知识内容）——只读不自动写：
# v9.15.2 的自动学噪音钩子把老师的批评句和"关系"等核心概念拉黑了，已移除。
# 老师想屏蔽/合并某个词，用 [RULE]（见 brain/PROTOCOL.md），可审计可撤销。
try:
    FORMAT_NOISE = set(json.load(open(NOISY_WORDS_FILE, encoding="utf-8")))
except Exception:
    FORMAT_NOISE = set()

# 功能词停用表（成词但无概念价值；jieba 会切出大量这类单字/虚词）
STOP = {
    "的", "了", "是", "在", "有", "和", "与", "及", "或", "也", "都", "就", "而",
    "等", "之", "其", "这", "那", "你", "我", "他", "它", "们", "什么", "怎么",
    "为什么", "一个", "没有", "可以", "就是", "不是", "已经", "因为", "所以",
    "但是", "而且", "如果", "虽然", "然后", "还有", "只是", "还是", "自己",
    "这个", "这样", "这里", "那个", "那样", "那里", "哪些", "哪些东西", "哪些方面",
    "可能", "一些", "一下", "一定", "以及", "对于", "关于", "通过", "使得",
    "认为", "知道", "时候", "东西", "方面", "情况", "方式", "过程", "问题",
    "请问", "请讲", "谢谢", "feed", "FEED", "我们", "我的", "你的", "它的", "它们", "他们",
}
# 注：STOP 收敛在虚词/指代/套话；"问题"一词对话噪音大于概念价值，先停用（可调）。

_FALLBACK_RE = re.compile(r"[\u4e00-\u9fa5]{2,6}|[a-zA-Z]{4,}")


def raw_tokens(text):
    """全量切词（不过滤）。jieba 可用走真分词；否则回退旧滑窗。"""
    if not text:
        return []
    if HAS_JIEBA:
        try:
            return _jieba.lcut(text)
        except Exception:
            pass
    return _FALLBACK_RE.findall(text)


def is_word(w):
    """可作为概念词的形状：2-8 字中文，或 ≥4 字母英文。"""
    if not w:
        return False
    if re.fullmatch(r"[\u4e00-\u9fa5]{2,8}", w):
        return True
    return bool(re.fullmatch(r"[a-zA-Z]{4,}", w))


def concept_words(text, limit=None):
    """文本 → 可用概念的 Counter（去停用词/格式噪声/非词形状）。
    回退模式下顺带做一趟子串合并（近似 jieba 的边界效果）。"""
    from collections import Counter
    toks = [t.strip() for t in raw_tokens(text) if t and t.strip()]
    c = Counter()
    for t in toks:
        if not is_word(t) or t in STOP or t in FORMAT_NOISE:
            continue
        c[t] += 1
    if not HAS_JIEBA:
        c = _merge_substrings(c)
    if limit:
        return Counter(dict(c.most_common(limit)))
    return c


def _merge_substrings(counter):
    """回退模式的碎片合并：短词若是某更长词的子串，把计数并给长词。"""
    words = sorted(counter.keys(), key=len, reverse=True)
    out = {}
    for w in words:
        n = counter[w]
        for long_w in out:
            if w != long_w and w in long_w:
                out[long_w] += n
                n = 0
                break
        if n:
            out[w] = n
    from collections import Counter
    return Counter(out)


def file_topic_words(lines, n=8):
    """文件首部 → 摘要关键词串（替代旧 regex；供 last_kw / 笔记展示）。"""
    c = concept_words(" ".join(lines))
    return [w for w, _ in c.most_common(n)]
