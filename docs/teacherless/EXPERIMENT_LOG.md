# Teacherless 实验日志

记录规则：

- 只记录**实际执行过**的实验与**实际观测到**的结果。
- 每个条目必须写清：日期、阶段、假设、环境、命令、结果、解释、失败/异常、下一步。
- 不把设计中的功能写成已运行；不把轮次、概念数、情绪值当作学习进步的证据。

---

## 2026-10-25 / Phase 0：代码审计

- 阶段：0
- 目标：确认真实执行链、教师/LLM 入口、隐性继承路径，确定阶段 1 的最小隔离范围。
- 方法：逐文件阅读 `selfdrive/`、`semantics.py`、`agent_v9.py`、测试；追踪函数调用关系；
  不连接云端，不修改运行状态。
- 主要发现：
  1. 教师入口不止 `ask_brain`：`read_brain`、模板回填、`compiled_rules`、
     `infer_log.teacher_verdict`、共享向量库、外部目录扫描、兄弟信箱、semantics 指纹
     都会把教师历史带进新实验。
  2. 决策层和记忆层会读取 `brain/outbox`，因此只禁用两个技能不足以隔离。
  3. 最大的继承风险是旧 `state.json`；必须独立初始化并记录种子来源。
- 产出：`docs/teacherless/PHASE0_AUDIT.md`。
- 结论：先做阶段 1 硬隔离，不做研究/搜索/验证器重构。

---

## 2026-10-25 / Phase 1：硬隔离实现与自动化测试

- 阶段：1
- 假设：在独立目录、独立 state、显式 `SELFDRIVE_MODE=teacherless` 下，
  系统可以在教师和 LLM 零调用的条件下持续运行，且不会把旧教师知识记成新学到的知识。
- 实现：
  - 新增 `selfdrive/teacherless.py`（manifest、种子账本、污染扫描、基线计数、审计、CLI）。
  - `config.py` 增加模式开关；Teacherless 下强制关闭 LLM、兄弟信箱、外部目录扫描。
  - `main.py` 启动前验 manifest/state，加载后写基线，轮末再验；standard 模式拒绝
    Teacherless state。
  - `skills.py` 五个通道执行层硬拒绝；`decision.py` 权重清零作为第二道防线；
    `working_memory.py`、`templates.py`、`conceptcard.py` 不再读教师通道；
    `llm_bridge.py`、`kb.py` 全禁。
- 测试命令：

```bash
py -m compileall -q selfdrive agent_v9.py tests
py -m unittest discover -s tests
```

- 测试结果：
  - 编译通过。
  - `Ran 80 tests ... OK`。
  - 其中 Teacherless 专项 42 项（阶段 1 当时；阶段 1.1 后为 61 项，见下），
    覆盖初始化拒绝、种子策略、基线增长、污染标记、被禁通道事件、5 个技能零调用、
    LLM 强开仍零调用、共享库禁用、工作记忆/决策/模板/概念卡门禁、standard 模式回归。
- 解释：这是**代码级隔离证据**，不是学习能力证据。

---

## 2026-10-25 / Phase 1：独立目录冒烟（v2，最终代码）

- 阶段：1
- 假设：在完全没有教师历史、没有训练资料、没有 LLM 的全新目录中，
  系统至少能连续运行两轮，且所有教师/LLM 计数保持为 0。
- 环境：Windows 10，`py` 启动器；目录 `runs/teacherless_smoke_20261025_v2/`
  （gitignored）；只复制 `selfdrive/*.py`、`semantics.py`、`agent_v9.py`。
- 初始化：

```bash
py -m selfdrive.teacherless init --experiment TL-SMOKE-20261025-V2 --name 无师
```

- 初始化结果：
  - `seed_policy=from_zero`
  - `clean_from_zero=true`
  - `llm_enabled=false`
  - `channels_disabled=[ask_brain, explore_wider, llm_consult, read_brain, sibling_letter, shared_kb]`
  - 代码指纹、内置模板/概念卡/L001/权重/情绪默认值作为 `base_code` 先验写入 manifest。
- 运行命令（故意把 LLM/兄弟环境变量开到最大，验证硬门禁优先于环境变量）：

```bash
SELFDRIVE_MODE=teacherless SELFDRIVE_LLM=1 SELFDRIVE_SIBLING=moon py -u agent_v9.py --once
```

- 运行结果：
  - 轮 1：主动作 `self_talk`；辅动作 `build_site`、`reflect_memory`。
  - 轮 2：主动作 `tidy_notes`；辅动作 `rest`、`self_talk`。
  - `logs/events.jsonl` 共 6 条；被禁通道事件 0 条。
  - `artifact_counts`：8 项全部 0（教师问题、教师回答、教师规则、教师裁决、
    LLM 提案、LLM 调用、兄弟信件、外部扫描文件）。
  - `baseline_counts`：8 项全部 0。
  - `violations={}`、`contamination_markers={}`、`blocked_channel_log_counts={}`。
  - `code_drift=false`。
  - `clean=true`。
  - `state.json`：`rounds=2`；`llm_proposals=0`；`brain_questions=0`；`brain_read=0`；
    `working_memory.goal=learn_and_verify`；`available_skills=11`。
  - 没有生成 `brain/inbox.md` 或 `brain/outbox/`。
- 标准模式误启动检查：

```bash
SELFDRIVE_MODE=standard py -u agent_v9.py --once
```

结果：进程以 `RuntimeError` 退出（exit=1），原 state 保持 `rounds=2`、`clean=true`。

- 异常记录：
  - 早先的同构冒烟（v1，代码尚未加入 `code_drift` 报告）中，`refresh_site` 在 Windows
    上报 `[WinError 2]`（找不到 `python3`）。v2 未触发该动作。此问题属于既有平台兼容性，
    不是教师/LLM 泄漏。
- 解释：
  - **隔离成立**：教师、LLM、兄弟信箱、外部扫描、共享向量库在运行期均为零。
  - **不能宣称学习**：两轮后 `concepts=0`、`claims=0`、`relations=0`。目录里没有训练资料，
    也没有留出任务；本实验只证明“能在无教师/无 LLM 条件下运行”。
- 产物：`runs/teacherless_smoke_20261025_v2/`（本地临时目录，不提交）。

---

## 2026-10-25 / Phase 1.1：审查修正（代码版本、L001 溯源、种子账本）

- 触发：owner 对提交 `47dafa2` 的审查，要求先完成两个 P1 与种子清单校验规则，
  再进入阶段 2。
- 代码修改：
  - `teacherless.py`：`verify_clean_start`/`assert_state_clean` 硬校验 `code_sha256`；
    `status.clean` 纳入代码漂移与种子问题；新增 `accept_code_change` 与
    `accept-code --reason`，迁移记录保留 from/to/版本/时间/原因；manifest 加载时
    重算 `seed_items_sha256`；外部种子强制 `path` 并登记 `sha256`/`bytes`/
    `file_count`/`imported_at`，可选 `import: true` 复制进 archive；`seed_problems`
    在启动与 status 复核种子文件。
  - `conceptcard.py`：L001 改为 `support=0`、`source=base_code_prior`、
    `confidence_basis=declared_prior_uncalibrated`；0.4 仅为声明初值。
  - `main.py`：启动审计记录固定代码指纹。
  - `README.md` / `docs/teacherless/*`：同步 `from_zero` 含义、种子规则、
    代码迁移流程与阶段边界。
- 自动化测试：
  - `py -m compileall -q selfdrive agent_v9.py tests`：通过。
  - `py -m unittest discover -s tests`：**99 项全部通过**。
  - `tests/test_teacherless.py`：**61 项**，新增代码漂移、迁移审计、种子路径/哈希/
    大小/导入、manifest 篡改、L001 先验等用例。
- 隔离冒烟 v3：`runs/teacherless_smoke_20261025_v3/`（本地临时目录，不提交）。
  - `from_zero` 初始化；`code_sha256_initial == code_sha256`；`code_migrations=[]`。
  - 3 轮；`status`：`clean=true`、`code_drift=false`、`seed_problems=[]`；
    8 项教师/LLM 痕迹全 0；无污染标记与被禁通道事件。
  - `concepts=0`、`claims=0`、`relations=0`；`brain_questions/brain_read=0`；
    `llm_proposals=0`；`available_skills=11`。
  - L001：`support=0`、`source=base_code_prior`、`confidence=0.4`、
    `confidence_basis=declared_prior_uncalibrated`。
- 代码漂移冒烟（v3 副本）：
  - manifest 指纹改全 0 后启动：`RuntimeError`，exit=1，state 仍为 `rounds=3`。
  - `accept-code --reason "..."`：写入迁移记录；随后 `status` 为
    `clean=true`、`code_drift=false`、`code_migrations=1`。
- 异常：第 3 轮 `build_site` 触发既有 Windows `[WinError 2]`（找不到 `python3`），
  不影响隔离结论。
- 解释：本阶段只证明隔离、代码版本固定、种子可核验与迁移可审计；
  **不证明学习**。训练资料与留出任务仍为 0。

## 2026-10-25 / Phase 2：研究任务队列与定向搜索规划（第一小步）

- 阶段：2
- 触发：owner 审查通过 Phase 1.1（提交 `19076a3`），授权只实现“研究任务队列 +
  定向搜索规划”，不得重写 claims/预测/统一验证器/整个决策中枢。
- 假设：把“搜索最近出现的词”改为“从知识缺口生成任务，再按目的规划查询”，
  可以让每次搜索都可解释、可去重、可追溯，同时不把搜索结果误当成已验证知识。
- 实现：
  - 新增 `selfdrive/research.py`：任务队列（五类来源、稳定 ID、优先级、状态机、
    上限与修剪）、查询规划（每任务最多 3 个不同目的）、完全/Jaccard 双重去重、
    `no_results` 单次改写、`network_error`/`serp_blocked` 换目的、来源账本、
    配置快照 `config_history`。
  - `main.py` 每轮刷新任务；`decision.py` 有开放任务时轻微抬升 `web_search`
    权重；`working_memory.py` 暴露只读 `research_focus`。
  - `skills.do_web_search` 改为研究查询消费者；新增 Bing SERP 解析、主机回退
    （`cn.bing.com` → `www.bing.com`）、跳转链还原（`/ck/a?...u=a1<base64>`）；
    成功只写 `workspace/web/*.md` 与 `unverified` 候选材料。
  - `state.py` 增加 `research` 段（`STATE_VERSION=4`），版本号 `9.24`。
- 边界：
  - 不调用 `claims.add_evidence`，不更新 `laws`，不修改概念/命题置信度；
  - 搜索结果 `source_type=web_runtime`、`verification=unverified` 硬编码；
  - `materials_found` 仅表示“有候选材料待验证”，阶段 3 才接证据门禁。
- 自动化测试：
  - `py -m compileall -q selfdrive agent_v9.py tests`：通过。
  - `py -m unittest discover -s tests`：**129 项全部通过**
    （阶段 1.1 为 99 项，阶段 2 新增 30 项）。
  - `tests/test_research.py` 覆盖 owner 指定案例：同一目标多目的查询、重复主题抑制、
    失败改写/换目的、查询→任务、来源→查询追溯；另覆盖任务/尝试/配置上限、
    工作记忆只读、决策加权、Teacherless 零痕迹。
- 回退防线（v4 重建前发现）：`_pick_search_topic` 的旧回退链会把日记尾词/文件名
  当成主题，从零目录里实际生成了一个“涌现”探索任务，等于重新引入“搜最近的词”。
  现在 `do_web_search` 要求回退主题必须已晋升为概念，否则返回无查询；
  `test_unbacked_recent_topic_is_not_searched` 固定该行为。
- 隔离冒烟 v4（最终代码重建）：`runs/teacherless_smoke_20261025_v4/`
  （本地临时目录，不提交）。
  - `from_zero` 初始化，代码版本 `9.24`，代码指纹
    `3e57179e75bab4c68090c05b1e523678a2635d5fb7b3fdcc3f52f691d1826d04`，3 轮。
  - `status`：`clean=true`、`code_drift=false`、`seed_problems=[]`、
    `code_migrations=[]`；8 项教师/LLM 痕迹全 0；`violations={}`。
  - `research.stats` 全 0（任务/查询/来源均为 0），`research.jsonl` 仅
    `config_recorded` 1 条。
  - 3 轮后只有 1 个未晋升概念（`index`，n=1）、0 命题、0 关系，没有满足任务
    生成条件的缺口——这是正确行为。阶段 2 不会为了“看起来在工作”而凭空搜索。
- 真实搜索链路验证：`runs/research_harness_20261025/`（本地临时目录，合成一条
  `光|影` 弱关系；**不是学习实验**）。
  - 任务：`relation_unverified`（w=1,n=1），查询按 relation/evidence/counterexample
    目的推进；每次执行都带 task_id/query_id/purpose/selection_reason。
  - 5 次执行中 3 次成功，每次 5 条候选材料；Bing 跳转链已还原为 zhihu/zhidao
    原站 URL；来源记录含 `serp_url`、`fetched_at`、`content_sha256`、任务/查询 ID、
    当时的 `config_sha256`。
  - 该验证运行在最终回退防线之前，并暴露了问题：任务耗尽后，探索回退把日记尾词
    「下次」当成主题去搜索。防线加入后，无概念支撑的回退主题不再产生查询
    （最终 v4 重建中研究统计保持为 0）。
  - 观测到 Bing 会间歇返回首页/反爬页（记 `serp_blocked`，不触发改写）、
    真实无结果页（记 `no_results`，触发一次 `broadened` 改写）、以及
    TLS `UNEXPECTED_EOF`（主机回退后成功）。
  - 观测到搜索结果可能与查询主题无关（如“涌现是什么”返回 Gmail 链接）。
    这验证了阶段 2 的核心约束：**搜索成功只代表拿到候选材料，不代表知识成立**。
- 异常/限制：
  - Bing 可用性与结果相关性不稳定；已做主机回退和失败分类，但不能保证相关性。
  - 未做正文提取、来源可信度、证据计数与留出评估。
  - 不能据此宣称“搜索让系统变强”。
- 产物：`docs/teacherless/PHASE2_RESEARCH.md`、`tests/test_research.py`。

## 下一门槛

1. owner 检查 Phase 2 第一小步（研究任务、定向查询、去重/失败改写、来源账本、
   配置快照）与文档，确认是否进入阶段 3。
2. 阶段 3 前必须先确定：
   - 候选材料如何进入证据门禁（正文提取、来源可信度、同源去重、支持/反驳计数）；
   - 命题/关系边更新的可撤销记录与回滚方式；
   - 留出任务与对照评估的设计，避免把“搜到更多网页”当成能力提升。
3. 正式开跑前冻结代码版本；升级只能 `accept-code` 留下迁移记录，
   重大算法变更新建实验。
4. 在留出任务评估建立之前，任何“学会/进化”表述都不成立。
