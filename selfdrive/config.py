# -*- coding: utf-8 -*-
"""config — 路径 / 常量 / 注册表。只放声明，不放逻辑（v9.16 从 agent_v9.py 拆出）。"""
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # 项目根（selfdrive/ 的上一级）

WORKSPACE = BASE / "workspace"
ARCHIVE = WORKSPACE / "archive"
NOTES = WORKSPACE / "notes"
WEB = WORKSPACE / "web"
BRAIN = BASE / "brain"
INBOX = BRAIN / "inbox.md"
OUTBOX = BRAIN / "outbox"
TEMPLATES_DIR = BRAIN / "templates"          # 可插拔提问模板库（每个 .txt = 一个模板）
TEMPLATE_STATS_FILE = BRAIN / "template_stats.json"
EMOTIONS_CFG_FILE = BRAIN / "emotions.json"
NOISY_WORDS_FILE = BRAIN / "noisy_words.json"
LOGS = BASE / "logs"
STATE_FILE = BASE / "state.json"

INTERVAL = int(os.environ.get("SELFDRIVE_INTERVAL", "180"))   # v9.3：默认 180s（owner 拍板）
ONCE = "--once" in sys.argv

# ---- v9.16 ①：世界重开——web 自食缓存读入配额 ----
WEB_SHARE_CAP = 0.20        # 最近 RECENT_READS_MAX 次阅读中，web-* 占比上限
RECENT_READS_MAX = 100      # 阅读来源滑窗长度

# ---- v9.16 ②：可失败预测 ----
PRED_HORIZON = 60           # v9.16.1: 400→60（青梧诊断：12注从未结算；60轮=30分钟内必见分晓）
PRED_MAX_PENDING = 20       # 最多同时在赌的预测数
PRED_SETTLE_PER_ROUND = 3   # v9.16.1: 单轮最多结算笔数（防一次性痛感洪水）

# ---- v9.16 ③：[RULE] 编译器安全边界 ----
RULE_MAX = 30               # 编译规则条数上限（LRU 淘汰）
MULTIPLIER_MIN, MULTIPLIER_MAX = 0.05, 3.0   # 技能权重乘子夹紧范围

# ---- v9.16 ④：情绪痛感基线（不依赖老师，代码自带；[RULE] 可覆盖加大） ----
PAIN_DUP_ASK = -0.07        # 重复提问 → satisfaction
PAIN_PRED_MISS = -0.05      # 预测失误 → satisfaction（失误同时给好奇心加燃料）
FUEL_PRED_MISS = 0.05       # 预测失误 → curiosity（哪里猜错哪里有东西可学）
JOY_PRED_HIT = 0.06         # 预测命中 → satisfaction
NOVELTY_WEB_SCALE = 0.35    # 读自缓存时新意增益缩放（外部新知才是真新意）

# ---- 轮事件标签（④ 的钩子 & ③ 的触发词表；老师可用自然语言同义词，见 PROTOCOL.md） ----
EVT_ANY = "任何"
EVT_ASK = "提问"
EVT_DUP_ASK = "重复提问"
EVT_READ_ANSWER = "读回答"
EVT_READ_CACHE = "读自缓存"
EVT_PRED_HIT = "预测命中"
EVT_PRED_MISS = "预测错误"
EVT_INFER_FALSE = "推理判假"
EVT_SEARCH = "搜索"

# ---- 技能注册表（v9 接口不变：感知/行动声明 + 成本 + 情感系数） ----
SKILLS = {
    "explore_file":  {"sense": ["workspace/archive 文件"], "act": ["读全文", "全文入库kb"],
                      "cost": 0.4, "weight": {"base": 0.2, "curiosity": 1.0, "curiosity_x_boredom": -1.0}},
    "explore_wider": {"sense": ["外部目录文件"], "act": ["扫描", "读取"],
                      "cost": 0.5, "weight": {"base": 0.01, "curiosity": 0.9}},
    "visit_new_dir": {"sense": ["目录列表"], "act": ["遍历目录"],
                      "cost": 0.2, "weight": {"base": 0.01, "curiosity": 0.7}},
    "tidy_notes":    {"sense": [], "act": ["整理笔记"],
                      "cost": 0.1, "weight": {"base": 0.1, "satisfaction": 0.4, "boredom": 0.5}},
    "build_site":    {"sense": [], "act": ["部署网站"],
                      "cost": 0.6, "weight": {"base": 0.05, "boredom": -0.3}, "bonus_not_built": 0.5},
    "refresh_site":  {"sense": [], "act": ["刷新网站"],
                      "cost": 0.2, "weight": {"base": 0.05, "satisfaction": 0.6}, "need_built": True},
    "ask_brain":     {"sense": ["未读回答"], "act": ["写问题到inbox"],
                      "cost": 0.5, "weight": {"base": 0.0, "curiosity": 0.6, "boredom": 0.2}},
    "read_brain":    {"sense": ["outbox回答"], "act": ["读回答", "入库"],
                      "cost": 0.3, "weight": {"base": 0.15, "satisfaction": 0.4}},
    "rest":          {"sense": [], "act": ["休息"],
                      "cost": 0.0, "weight": {"base": 0.2, "fatigue": 2.0, "curiosity": -0.2}},
    "web_search":    {"sense": ["互联网"], "act": ["搜索", "抓取摘要"],
                      "cost": 0.5, "weight": {"base": 0.0, "curiosity": 0.5, "boredom": 0.15}},
    "consolidate_memory": {"sense": ["旧笔记"], "act": ["重放", "修剪规则"],
                      "cost": 0.2, "weight": {"base": 0.05, "satisfaction": 0.3, "boredom": 0.25}},
    "reflect_memory":  {"sense": ["自己的记忆"], "act": ["回想", "关系推理"],
                      "cost": 0.2, "weight": {"base": 0.12, "satisfaction": 0.3, "boredom": 0.3, "curiosity": 0.15}},
    "self_talk":     {"sense": ["自己的记忆"], "act": ["自言自语", "内部推理"],
                      "cost": 0.1, "weight": {"base": 0.08, "satisfaction": 0.2, "curiosity": 0.35}},
}

# ---- 情感默认配置（brain/emotions.json 不存在时写入；存在则以文件为准） ----
DEFAULT_EMOTIONS_CFG = {
    "curiosity": {
        "default": 0.3, "decay": 0.04, "novelty_gain": 0.12,
        "grow": {"build_site": 0.15, "read_brain": 0.1},
        "shrink": {"ask_brain": 0.05},
        "desc": "好奇心：遇到新东西上升，提问后小幅回落",
    },
    "boredom": {
        "default": 0.1, "decay": 0.02,
        "grow": {"explore_file": 0.06, "tidy_notes": 0.06, "refresh_site": 0.06},
        "shrink_other": 0.08, "extra_grow_curiosity_low": 0.05,
        "desc": "无聊：重复性动作积累，好奇心太低时额外上升",
    },
    "satisfaction": {
        "default": 0.2, "decay": 0.03, "success_gain": 0.15,
        "grow": {"build_site": 0.4, "read_brain": 0.3},
        "desc": "满足感：成功动作上升（v9.16 起成功增益按新意缩放，重复不再白涨）",
    },
    "fatigue": {
        "default": 0.0, "decay": 0.0, "non_rest_gain": 0.05, "rest_change": -0.35,
        "desc": "疲劳：非休息动作积累，休息大幅回落",
    },
}

# ---- 提问模板：种子 + v9.16 内置"相反/先后"两件（补 contrast/temporal 表征入口） ----
SEED_TEMPLATES = [
    "我读了《{topic}》，它的核心意思是什么？有什么我没注意到的要点？",
    "我探索了这些资料，它们之间有什么联系？",
    "除了我现在读的这些东西，还有哪些值得探索的方向？",
    "我现在应该尝试做什么新的事情？",
    "《{topic}》的反面或对立面是什么？什么情况下它会不成立？",
    "要出现《{topic}》，之前通常需要什么条件？它之后又会引出什么？（说说先后）",
]
# 内置模板 id（文件名 stem）——模板演化永不淘汰它们
BUILTIN_TEMPLATE_IDS = {"seed-contrast", "seed-temporal"}
CONTRAST_TPL_IDS = {"seed-contrast"}
TEMPORAL_TPL_IDS = {"seed-temporal"}

# 模板库演化参数
TEMPLATE_MAX = 15
PRUNE_EVERY = 25
PRUNE_KEEP_MIN = 5

# ---- 外部扫描（explore_wider / visit_new_dir） ----
WIDER_ROOTS = ["/home/qingwu", "/usr/share/doc"]
WIDER_EXTS = {".txt", ".md", ".py", ".json", ".html", ".rst", ".log"}
# v9.16.1: 补 site-packages/dist-packages（kbvenv 的库文件曾被整目录扫进清单）、
# selfdrive_data（服务器数据副本目录）、runs（冒烟沙箱）
BLACKLIST_DIRS = {".ssh", ".cache", ".git", "venv", ".venv", "__pycache__", "node_modules",
                  ".config", ".local", ".npm", ".java", ".julia", ".rustup", ".cargo",
                  "kbvenv", "kb_data", "selfdrive", "agent-audit",
                  "site-packages", "dist-packages", "selfdrive_data", "runs"}

# ---- 搜索主题词黑名单（沿用 v9.9） ----
SEARCH_BLACK_WORDS = {"密码", "爆破", "入侵", "漏洞", "赌博", "色情", "毒品", "武器", "黑客"}

# ---- 辉月回望：相反词对种子（v9.8） ----
CONTRAST_PAIRS = [
    ("有序", "无序"), ("秩序", "混沌"), ("信号", "噪声"), ("信息", "噪声"),
    ("确定性", "不确定性"), ("简单", "复杂"), ("局部", "全局"),
    ("微观", "宏观"), ("随机", "规律"), ("吸引", "排斥"),
]

# ---- v9.18 一层：并行扩散激活（spreading activation，Collins&Loftus 1975 / 全局工作空间赢家通吃） ----
SA_PATHS = 4              # 每轮思考并行撒出的联想路径数
SA_MAX_STEPS = 2          # 每条路径最多走几步
SA_NOVELTY_EXP = 1.5      # 新鲜感指数（对抗强边垄断——v9.15 同义反复的教训）
SA_WIN_REPEAT_CAP = 3     # 同一赢家连续浮上来的容忍次数，超过则临时降权
SA_NOVELTY_NORM = 1000.0  # 新鲜感归一化：距上次走过的轮数 / 此值

# ---- v9.18 三层：做梦 / 默认网络（DMN；Cai2009：梦游起点必须先被 prime） ----
DREAM_PROB_REST = 0.6     # rest 轮触发梦游的概率
DREAM_WALK_STEPS = 3      # 梦游步数（沿弱边）
DREAM_WEAK_W = 2          # 弱边定义：w <= 此值
DREAM_PRIME_WINDOW = 400  # "最近被激活过"的 priming 窗口（轮）
DREAM_LOG_MAX = 50        # dream_log 容量

# ---- v9.18 二层：分层图（CLS：底=海马式具体图，上=新皮层式抽象节点） ----
ABSTRACT_MIN_MEMBERS = 3      # 抽象节点最少绑定底层概念数
ABSTRACT_MAX_MEMBERS = 12     # 成员上限——巨型连通分量（摘要w=67式枢纽污染）不成类
ABSTRACT_EDGE_W = 3           # 进入聚类的边权门槛
ABSTRACT_PER_CONSOLIDATE = 2  # 每次巩固最多新生成的抽象节点数
ABSTRACT_MAX = 40             # 抽象节点总量上限
ABSTRACT_LINK_SCORE = 0.8     # 经抽象节点的跨层激活折扣

# ---- v9.19 错误闭环 / 类比接地 / 元认知 ----
REPLAY_QUEUE_MAX = 6          # 错误重放队列容量（猜错的对儿等巩固期对拍）
PAIN_PRED_MISS_V2 = -0.08     # 预测落空痛感（v9.19 加倍——活物猜错要愣一下）
FUEL_PRED_MISS_V2 = 0.08      # 预测落空的好奇心燃料（困惑）
ABSTRACT_GROUND_MIN = 2       # 类比接地门槛：抽象节点须有 ≥N 次验证经历才准参与跨层类比
META_EVERY = 60               # 元认知自省间隔（轮）
TRANSFORM_RECHECK_SUPPORT = 1  # 老变换复检：support ≤ 此值且从未复检过的，择机重问大脑

# ---- v9.19.1（晨间报告两修正） ----
BET_PER_CONCEPT_CAP = 2       # 新概念磁铁效应：同一概念的 pending 注上限（减法×34 的教训）
DREAM_BET_HORIZON = 120       # 梦注有效期加倍（梦押本就冷门，60 轮太苛刻）


def load_ask_config():
    """读 brain/ask_config.json。失败返回 {}（各处用默认值，行为回退）。"""
    try:
        with open(BRAIN / "ask_config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
