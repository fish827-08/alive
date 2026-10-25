# Alive — 自主决策中枢实验（辉月、moon & 火星）

> 三个人工生命（数字小生命）在一个文本沙箱里出生、成长、犯错、被教导。
> **没有 LLM 驱动它们的核心**——驱动它们的是 2500 行手写的符号/统计代码；
> LLM 只在明确划定的边界内作为"提案生成器"参与（可插拔、可关闭）。
> 它们不会写文章，但它们会：自己归纳加法变换、对无边概念下注并被世界判对错、
> 做梦、向老师提问、把老师的批评变成制度、以及——问出你没想到的问题。
>
> 另有**无教师实验基线**（`SELFDRIVE_MODE=teacherless`）：独立目录、独立状态，
> 硬禁用教师与 LLM 通道，用于回答"没有老师时它还能学什么"。目前完成阶段 1.1
> （隔离性、种子来源与代码版本可核验）与阶段 2 第一小步（研究任务队列 + 定向搜索 +
> 候选材料来源账本）；证据门禁、统一验证器与留出评估尚未实现。

---

## 这是什么

一个**活体认知实验**：让一个可以完全读穿的小认知系统连续运行数万轮，观察它
"学会理解"的全过程——包括它生病的样子。所有认知事件（概念晋升、定律毕业、
预测结算、错误清除、做梦、兄弟通信）都写进可审计的日志。

当前三个学生**都有教师**，差异在教师风格与是否启用 LLM：

| 学生 | 位置 | 轮龄 | LLM | 教学 |
|---|---|---|---|---|
| **辉月** | 云端 `/home/qingwu/selfdrive` | 7000+ | 无（路线 A） | 教师制·进修式（[VERDICT] 判卷 / [RULE] 制度） |
| **moon** | 云端 `/home/qingwu/moon` | 1400+ | 无（路线 A） | 教师制·幼儿式（日常问答与纠错） |
| **火星** | 本机 `D:\火星` | 从零 | qwen3:1.7b（路线 B） | 教师制·幼儿式 + LLM 提案 |

## 架构（v9.24.3，模块化）

```
selfdrive/            # 认知包（所有逻辑在这里，agent_v9.py 只是启动器）
├── config.py         # 常量/技能注册表/LLM 开关
├── main.py           # 主循环（每轮编排 13 道决策门）
├── decision.py       # 候选后果评估 + 情感/内稳态/信息价值/风险仲裁
├── working_memory.py # 容量受限工作记忆（目标、缺口、证据、候选与选择）
├── research.py       # 研究任务队列 + 定向查询规划 + 候选材料来源账本
├── outcomes.py       # 行动前预期→行动后实际结果→Brier 校准与信用归因
├── skills.py         # 15 个技能实现（探索/问答/搜索/巩固/做梦/信箱/LLM…）
├── tokenizer.py      # jieba 分词器官（缺失自动回退）
├── learning.py       # 概念晋升制/赫布建边/定律归纳/碎片合并/老变换复检
├── claims.py         # 命题与独立证据门禁、来源校准、证据日志
├── probes.py         # 可执行环境探针（封闭中文数词加法）
├── verification.py   # 教师意见与环境裁决汇合（探针优先）
├── conceptcard.py    # 七面概念卡 + Laws 定律表（提案/验证/毕业/反驳降级）
├── predictions.py    # 可失败预测（无边概念对下注→同现兑现/超时落空）
├── activation.py     # 并行扩散激活（4 路径撒网，赢家通吃，防垄断）
├── abstraction.py    # 分层图（巩固期自发生长抽象节点）
├── dream.py          # 做梦/默认网络（primed 起点沿弱边梦游→弱关联下注）
├── rules_compiler.py # [RULE] 陈述→动作编译器（老师的话直接进决策层）
├── llm_bridge.py     # LLM 提案桥（Ollama；只提案不掌权，失败静默降级）
├── emotions.py       # 情感演化/昼夜节律/能量内稳态
├── templates.py      # 提问模板库 + 查重
├── teacherless.py    # 无教师实验：独立初始化/种子溯源/污染拒绝/运行期审计
├── state.py / kb.py  # 状态持久化 / 向量记忆库客户端
```

**四权分立**（安全边界）：LLM/教师/做梦只有**提案权**——输出一律是待验证提案；
**验证权**归探针与阅读消化（概念晋升制，同源重复不膨胀置信度）；**信念权**归
中枢门禁；**行动权**归 `pick_action` 的候选后果仲裁。决策模式可用
`SELFDRIVE_DECISION_MODE=legacy|shadow|enforce` 切换，默认 `enforce`。

## 快速开始

### 云端部署（辉月 + moon，无 LLM）

```bash
# 服务器（ubuntu）：
git clone https://gitee.com/little-fishy/alive.git ~/selfdrive && cd ~/selfdrive
python3 -m pip install jieba
# 两个独立工作目录（目录名决定身份：selfdrive=辉月，moon=moon）
git clone https://gitee.com/little-fishy/alive.git ~/moon
mkdir -p ~/selfdrive/logs ~/moon/logs
# 串行调度器（两个 agent 一轮接一轮，永不同时占 CPU）
cat > ~/alternator.sh <<'EOS'
#!/bin/bash
while true; do
  (cd ~/selfdrive && python3 -u agent_v9.py --once >> logs/console.txt 2>&1)
  (cd ~/moon && python3 -u agent_v9.py --once >> logs/console.txt 2>&1)
  sleep 5
done
EOS
tmux new-session -d -s agents "bash ~/alternator.sh"
```

单 agent 快速试跑：`python3 agent_v9.py --once`（跑一轮），
或 `SELFDRIVE_INTERVAL=30 python3 agent_v9.py`（30 秒/轮常驻）。

### 本机学生「火星」（带 LLM）

1. 安装 [Ollama](https://ollama.com) 并拉模型：`ollama pull qwen3:1.7b`
2. 建学生目录（名字即它的名字）：

```bash
mkdir D:\火星   # 目录名会成为它的名字
# 复制：selfdrive/ 包、agent_v9.py、semantics.py 过去
# 建空目录：logs/、brain/outbox/、workspace/notes/
```

3. 启动（环境变量开启 LLM）：

```bash
cd D:\火星
set SELFDRIVE_LLM=1                      # Windows；Linux 用 export
set SELFDRIVE_LLM_MODEL=qwen3:1.7b
python agent_v9.py                       # 常驻；--once 单轮
```

LLM 的回答**不直接进概念库**——接口只接受含 `proposals`、`assumptions`、`unknowns`、
`suggested_checks` 四个字符串数组的 JSON，全部存为 `unverified` 提案；散文和越权字段会被拒绝。
模型不在线时自动降级为无 LLM 路径。火星需用 `SELFDRIVE_SIBLING=同伴目录名`
显式配置通信对象，不会默认冒充 moon。

### 无教师实验基线（Teacherless，阶段 1.1 + 阶段 2）

该模式硬禁用教师问答、教师回答读取、LLM、兄弟信箱、外部目录扫描与共享向量库，
并要求**独立工作目录 + 独立 state/logs**。它不会接管辉月/moon/火星的任何数据。

```bash
# 1) 建独立目录（不要复用已有 state/brain/workspace/logs/semantics 的目录）
mkdir ~/teacherless && cd ~/teacherless
cp -r /path/to/alive/selfdrive /path/to/alive/agent_v9.py /path/to/alive/semantics.py .

# 2) 初始化：from_zero 要求目录干净，并登记代码级种子先验
SELFDRIVE_MODE=teacherless python3 -m selfdrive.teacherless init --experiment TL-001

# 3) 运行与体检
SELFDRIVE_MODE=teacherless python3 agent_v9.py --once
SELFDRIVE_MODE=teacherless python3 -m selfdrive.teacherless status
```

Windows 用 `py` 代替 `python3`，并用 `set SELFDRIVE_MODE=teacherless` 设置环境变量。
`init` 会写 `teacherless_manifest.json`：记录代码指纹、种子来源、被禁通道与基线计数。
`from_zero` 指**未继承历史运行状态或预装经验语料**，但仍包含代码级先验
（内置模板、加法/能量概念卡、L001 假设、技能权重、情绪默认值）；这些先验在
`seed_items` 中逐项登记，不计为实验中学到的证据。

种子不是“只写一句声明”就算数：外部种子必须提供真实 `path`，初始化时记录
`sha256`、`bytes`、`file_count` 与导入时间；`import: true` 会复制进
`workspace/archive/`，否则只登记来源。manifest 的 `seed_items_sha256` 在每次加载时
重算，启动与 `status` 会复核种子文件内容——被改动、缺失或清单被篡改都会拒绝运行。
`teacher_history_seeded` 只能作为“停止教学后的自主延续”对照，不能再宣称“完全从零”。

实验期间代码版本固定：启动与每轮结束都会核对 `code_sha256`。代码变化后实验会停止，
不会把新旧算法混进同一段实验历史。确认升级时须显式记录迁移原因：

```bash
SELFDRIVE_MODE=teacherless python3 -m selfdrive.teacherless accept-code \
  --reason "说明本次升级改了什么、为什么必须现在改"
```

重大算法变更应新建实验，而不是在旧实验上原地升级。

**阶段 2 第一小步（研究任务 + 定向搜索）**：系统每轮从本地状态生成可解释的研究任务
（预测落空、命题证据缺口、弱关系未验证、孤立概念），按任务类型规划最多 3 条
**目的不同**的查询（找证据/找反例/查定义/查机制/查关系/找桥接），并做
“完全相同 + 二元组 Jaccard 相似”双重去重。`no_results` 允许一次更宽改写；
网络失败或返回首页/反爬页时换主机、换查询目的，不重复同一文本。每次查询都绑定
`task_id`/`query_id`/`selection_reason`，可回答“为什么搜这句”。同一关系先有弱关系
任务、后出现预测落空时，任务原地升级为 `prediction_miss`，旧尝试归档、查询/来源保留，
不会因旧任务占位而丢掉更强信号；探索回退与常规创建共用三组任务硬上限。
新建额度用尽只阻止继续新建，不会跳过后续既有任务的刷新/升级；阶段 2.0 的旧任务 ID
与来源字段在首次加载时显式迁移，查询/来源关联保留。迁移合并同一关系的新旧任务时，
任务种类取最强信号，生命周期取仍可执行的那条：有 `active`/`open` 任务就保持可执行，
只有待验证材料时落为 `materials_found` + `pending_verification`，不会用旧终止态
把可继续任务或候选材料埋掉。

搜索结果只保存为**候选材料**：登记真实 URL、标题/摘要、SERP URL/哈希、获取时间、
任务/查询 ID 与当时的配置哈希，硬编码 `source_type=web_runtime`、
`verification=unverified`。`title_excerpt_sha256` 只覆盖标题+摘要，**不是网页正文哈希**；
正文提取属于阶段 3。不写 `claims` 证据、不晋升定律、不改变概念置信度。影响研究调度与
搜索策略的配置（含 `ask_config.json` 的 `web_search`/`research` 段）会写入
`state.research.config_history`，只在哈希变化时记录。

Teacherless 模式下 `kb_remember()` 直接返回 `False`，候选材料不会进入共享向量库；
普通模式仍可能写入本地共享记忆库，阶段 3 不能把共享库召回自动视为已验证证据。

```bash
py -m unittest discover -s tests        # 140 项（阶段 2/2.1 新增 41 项）
```

详见 [docs/teacherless/PHASE2_RESEARCH.md](docs/teacherless/PHASE2_RESEARCH.md)。

**阶段边界**：当前完成阶段 1.1 的隔离性与种子/版本可核验性，以及阶段 2 的
研究任务/定向查询/来源账本；统一验证器、证据门禁、留出评估尚未实现，
不能据此宣称“已实现无教师自主学习”。
设计与审计见 [docs/teacherless/MASTER_PROMPT.md](docs/teacherless/MASTER_PROMPT.md)、
[docs/teacherless/PHASE0_AUDIT.md](docs/teacherless/PHASE0_AUDIT.md)、
[docs/teacherless/PHASE2_RESEARCH.md](docs/teacherless/PHASE2_RESEARCH.md)、
[docs/teacherless/EXPERIMENT_LOG.md](docs/teacherless/EXPERIMENT_LOG.md)。

### 老师如何教学（答疑协议）

学生把问题写进自己的 `brain/inbox.md`，老师把回答放进 `brain/outbox/Q-编号.md`。
结构化协议见 [brain/PROTOCOL.md](brain/PROTOCOL.md)，要点：

- `[VERDICT:真/假/存疑]` 提供教师判卷证据；算术由环境探针最终裁决，存疑不等于反驳
- `"X 的反面是 Y"` 句式会被收割成知识图的 typed 边
- `[RULE] 触发 -> 效果 [/RULE]` 直接编译进决策层（权重乘子/情绪/概念合并/新模板）
- `FEED-*.md` 主动投喂新概念

## 四个实验

1. **教学对照**：辉月（进修式教学）vs moon（幼儿式教学）——同环境同口粮，教学风格的差异
2. **路线对照**：云端纯无 LLM vs 本地 LLM 提案——LLM 只提提案时的增量
3. **消融**：任何模块（情绪/做梦/概念卡/LLM）没有可测贡献就移除，不因"像脑"保留
4. **无教师基线**：`teacherless` 模式下教师/LLM 零调用、独立状态、种子来源与代码版本可追溯；
   阶段 2 起搜索由研究任务驱动，结果只作为待验证候选材料。
   当前验收隔离性、账本完整性与查询可追溯性；是否有自主学习增益，要等阶段 3–4 的
   验证闭环与留出评估

实验 4 的判据是"无教师、无 LLM 条件下在未参与训练的任务上是否变强"，
不是轮次、概念数或日志体积。阶段 1.1/2 的冒烟与测试（140 项通过、隔离运行零违规、
来源账本可回溯）只能证明通道关闭、状态未污染、种子与代码版本一致、搜索材料有出处，
不能证明学习发生；阶段 2 的 `materials_found` 明确不等于“已验证”。

## 目录结构

```
agent_v9.py           # 启动器（12 行，全部逻辑在 selfdrive/ 包）
selfdrive/            # 认知包（见上）
brain/                # 辉月的师生通道（inbox/outbox/PROTOCOL.md/模板）
moon/                 # moon 的档案快照（云端运行时在 ~/moon）
docs/                 # 行为诊断报告与设计文档（从 v9.15 到当前架构设计）
  teacherless/        # 无教师实验：主提示词/阶段 0 审计/阶段 2 研究队列/实验日志
logs/                 # events.jsonl（每轮认知事件）+ console.txt
state.json            # 辉月的长期记忆（概念/关系/定律/概念卡/预测/履历）
workspace/            # 学生的"世界"（archive 口粮资料 + 自己搜的 web 缓存 + 笔记）
requirements.txt      # 唯一依赖：jieba（LLM 学生另需本机 Ollama）
```

## 观察它

- `logs/events.jsonl`：每轮的认知事件（含预测结算/做梦/规则编译/自省）
- `logs/research.jsonl`：研究任务/查询生命周期、去重拦截、失败改写、配置快照
- `logs/research_sources.jsonl`：每次搜索的候选材料来源（URL/摘要/哈希/SERP/任务与查询 ID，全部 unverified）
- `logs/teacherless_audit.jsonl`：仅 Teacherless 模式生成，记录初始化/启动/通道拦截
- `workspace/notes/journey.md`：它的日记（含"概念使用""定律毕业""自省"条目）
- 构建站：`build_site` 技能会生成 `workspace/web/index.html`（探索站网页）

## 哲学

**它记住了一个词 → 能把词对应到经验 → 能用经验预测 → 能通过干预验证 →
能迁移组合 → 能在错时修正 → 能在没有外部代答时持续用这些能力做决定。**

这条链的每一环都可测量。我们不做"它有没有意识"的断言，
只报告链条走到了哪一环、哪一环断了、断的那环怎么修。

详细设计见 [docs/自主决策中枢与理解架构设计.md](docs/自主决策中枢与理解架构设计.md)。
