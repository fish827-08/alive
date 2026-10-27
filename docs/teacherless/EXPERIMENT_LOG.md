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
    原站 URL；来源记录含 `serp_url`、`fetched_at`、当时的 `content_sha256`
    （Phase 2.1 已改名为 `title_excerpt_sha256`）、任务/查询 ID、
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

## 2026-10-25 / Phase 2.1：审查修正（任务竞争、哈希契约、创建上限）

- 阶段：2.1（不扩展功能，只修 owner 对 `a0ae54d` 指出的三个问题）
- 触发：owner 审查要求阶段 3 前先完成小型修正提交。
- 修正：
  1. **任务竞争**：关系任务改为按关系键唯一（`relation|A|B`）。
     `relation_unverified` 任务遇到同一关系的 `prediction_miss` 时**原地升级**：
     `kind`/问题/原因/目标切换为更强信号，旧 `attempts` 归档进 `attempt_history`，
     `rewrites` 重置，未执行查询标记 `cancelled`；`query_ids`/`source_ids`/
     `result_count` 保留。已 `exhausted`/`materials_found` 的任务也可被更强信号重开。
     升级写 `task_upgraded` 与 `kind_history`，不再用 `seen_relations` 跨类型压制。
  2. **哈希契约**：`content_sha256` 改名为 `title_excerpt_sha256`，明确它只覆盖
     标题+摘要（excerpt 最多 400 字符），不是网页正文；`SOURCE_FORMAT` 升为
     `alive-research-source/2`。`serp_sha256` 仍为 SERP HTML；正文提取属于阶段 3，
     当前不存在 `page_content_sha256`。
  3. **创建上限**：总任务 80、开放/活跃 24、每轮新增 3 的检查统一收口到
     `_create_task`，探索回退不再绕过；拒绝时写 `task_creation_blocked`。
  4. **文档副作用**：明确 `do_web_search` → `semantic_note` → `kb_remember`；
     Teacherless 下不写共享向量库，普通模式仍可能写入，阶段 3 不得把召回当已验证证据。
- 自动化测试：
  - `py -m compileall -q selfdrive agent_v9.py tests`：通过。
  - `py -m unittest discover -s tests`：**135 项全部通过**
    （阶段 1.1 为 99 项，阶段 2/2.1 为 36 项）。
  - 新增 6 项回归：弱关系任务被预测落空升级、耗尽任务重开且保留历史、
    `materials_found` 任务重开、探索回退遵守总/开放/每轮三组上限；
    来源哈希断言改为校验 `title_excerpt_sha256` 的输入与格式版本。
- 隔离冒烟 v5（Phase 2.1 最终代码）：
  `runs/teacherless_smoke_20261025_v5/`（本地临时目录，不提交）。
  - `from_zero`，代码版本 `9.24.1`，指纹
    `6d1811661e574f9234b07eea0a01018f4df84bcfbdc8dcd5516f8eb164085abb`，3 轮。
  - `status`：`clean=true`、`code_drift=false`、`seed_problems=[]`、
    `code_migrations=[]`、`violations={}`；8 项教师/LLM 痕迹全 0。
  - `research.stats` 全 0，`research.jsonl` 仅 `config_recorded`
    （`research-config-v2`）；3 轮后 0 概念/命题/关系/预测。
- 边界：仍不做正文提取、证据门禁、来源可信度、定律晋升与留出评估；
  不能据此宣称学习发生。

## 2026-10-26 / Phase 2.1 复审修正（预算扫描、旧状态迁移）

- 阶段：2.1（仍不扩展功能，不进入阶段 3）
- 触发：owner 复审 `38ff309` 指出两项边界问题。
- 修正：
  1. **预算不阻断扫描**：`ensure_tasks()` 在新建被 `_creation_block_reason()`
     拒绝后不再 `break`，而是记录一次 `task_creation_blocked` 并继续扫描；
     排在后面的既有任务仍可 `_refresh_task()` / 原地升级。创建上限只约束新建，
     不再因候选排序延迟高价值信号。
  2. **旧状态迁移（方案 A）**：`state.research.schema_version < 2` 时，
     `ensure_state()` 执行显式迁移：阶段 2.0 含 `kind` 的关系任务 ID 归一到
     关系键（`relation|A|B`）；查询与来源索引的 `task_id` 同步改写；旧来源
     索引 `content_sha256` 更名为 `title_excerpt_sha256`。同一关系的新旧任务
     碰撞时合并，旧尝试归档 `attempt_history`，查询/来源关联保留。迁移写
     `task_id_migrated` / `source_fields_migrated`，历史 JSONL 不重写。
     选择迁移而非“强制全新实验”，是为了让阶段 2.0 状态中的候选材料可继续
     被阶段 3 消费，同时不把旧关联混入未记录的新基线。
- 自动化测试：
  - `py -m compileall -q selfdrive agent_v9.py tests`：通过。
  - `py -m unittest discover -s tests`：**138 项全部通过**
    （阶段 1.1 为 99 项，阶段 2/2.1 为 39 项）。
  - 新增 3 项回归：本轮新建额度耗尽后既有弱关系任务仍被预测落空升级；
    旧关系任务 ID/查询/来源/旧字段迁移且幂等；新旧关系任务碰撞合并且
    历史尝试与关联不丢。
- 隔离冒烟 v6：`runs/teacherless_smoke_20261026_v6/`（本地临时目录，不提交）。
  - `from_zero`，代码版本 `9.24.2`，指纹
    `b3a2968d30fa20ec725f532ad52cdbac820003cfac077cdf61be65df51fba015`，3 轮。
  - `status`：`clean=true`、`code_drift=false`、`seed_problems=[]`、
    `code_migrations=[]`、`violations={}`；8 项教师/LLM 痕迹全 0。
  - `state.research.schema_version=2`，任务/查询/来源均为 0，`stats` 全 0；
    `research.jsonl` 仅 `config_recorded`（`research-config-v2`）；
    3 轮后 1 个未晋升概念（`index`，n=1）、0 命题/关系/预测。
  - 该冒烟为全新状态，不验证旧状态迁移；迁移由合成状态单测覆盖。
- 边界：仍不做正文提取、证据门禁、来源可信度、定律晋升与留出评估；
  不能据此宣称学习发生。

## 2026-10-26 / Phase 2.1 第三次复审修正（碰撞任务生命周期合并）

- 阶段：2.1（仍不扩展功能，不进入阶段 3）
- 触发：owner 复审 `6ba0116` 指出旧状态碰撞合并的生命周期缺陷——当同一关系的
  旧 `prediction_miss` 任务已 `exhausted`、旧 `relation_unverified` 任务仍
  `active` 或 `materials_found` 时，按种类优先级选主记录会把合并结果留在
  `exhausted`，可能同时带着 `pending_verification` 与候选材料；`_viable_tasks()`
  只选 `open`/`active`，这些材料会被埋掉。
- 修正：
  1. **种类与生命周期分开选**：`kind_source` 仍取最强研究信号（决定
     `kind`/问题/原因/目标），`carrier` 取仍可执行的任务（`active` 优先于
     `open`，其次更新时间与优先级）。合并以 `carrier` 为载体，其他任务的尝试
     归档进 `attempt_history`，`kind_history` 记录种类切换，
     `query_ids`/`source_ids`/`result_count` 合并保留。
  2. **无活跃任务时**：有候选材料则落为 `materials_found` +
     `pending_verification`，保留待验证入口；否则才按
     `blocked`/`exhausted`/`abandoned` 优先级保留终止态。
  3. 迁移后，可继续任务仍出现在 `_viable_tasks()` 中，待验证材料仍能被
     `focus()`/阶段 3 验证入口发现；不把冲突简单重置为 `open`，避免掩盖
     旧任务已经失败/阻断的事实。
- 自动化测试：
  - `py -m compileall -q selfdrive agent_v9.py tests`：通过。
  - `py -m unittest discover -s tests`：**140 项全部通过**
    （阶段 1.1 为 99 项，阶段 2/2.1 为 41 项）。
  - 新增 2 项回归：终止态 `prediction_miss` + 活跃 `relation_unverified`
    碰撞后，合并任务保持 `active`、可被 `_viable_tasks()` 选中且
    `next_query` 可用；终止态 `prediction_miss` + `materials_found`
    碰撞后，合并任务为 `materials_found` + `pending_verification`，
    结果数/来源保留且能在 `focus()` 中看到。
  - 另修正 `tests/test_teacherless.py` 子进程文本解码：显式
    `PYTHONIOENCODING=utf-8` + `encoding="utf-8"`，避免 UTF-8 locale 下
    父进程按 GBK 解码子进程 stderr 造成假失败；不改变运行逻辑。
- 隔离冒烟 v7：`runs/teacherless_smoke_20261026_v7/`（本地临时目录，不提交）。
  - `from_zero`，代码版本 `9.24.3`，指纹
    `8bc7366a1a23860763abd948ec1237e119a3ec5aaa7b497d811bf205fa449f93`，3 轮。
  - `status`：`clean=true`、`code_drift=false`、`seed_problems=[]`、
    `code_migrations=[]`、`violations={}`；8 项教师/LLM 痕迹全 0。
  - `state.research.schema_version=2`，任务/查询/来源均为 0，`stats` 全 0；
    `research.jsonl` 仅 `config_recorded`（`research-config-v2`）；
    3 轮后 0 概念/命题/关系/预测。
  - 第 2 轮 `build_site` 在本机启动服务失败（`[WinError 2]`，缺少启动器），
    属环境限制，不影响隔离与研究账本。
  - 该冒烟为全新状态，不验证旧状态迁移；迁移与碰撞生命周期由合成状态单测覆盖。
- 边界：仍不做正文提取、证据门禁、来源可信度、定律晋升与留出评估；
  不能据此宣称学习发生。

## 2026-10-26 / Phase 2.1 历史保留补丁（`attempt_history` 合并）

- 阶段：2.1（不扩展研究功能，不进入阶段 3）
- 触发：owner 短复审 `1f71102` 指出 `_merge_relation_task()` 只归档其他任务的
  当前 `attempts`，没有迁入其他任务已有的 `attempt_history`。旧版升级逻辑
  已经会产生该字段，因此碰撞迁移可能丢失部分历史尝试记录。
- 修正：
  1. 合并时同时收集主任务与对方任务的 `attempt_history`，再把对方当前
     `attempts` 归档为一条新历史；按 `round` 排序后只保留最近
     `HISTORY_KEEP=5` 条。
  2. 将散落的硬编码历史上限统一为 `HISTORY_KEEP`，明确这是有界保留，
     不是无界保存。
  3. `task_id_migrated` 记录 `attempt_history_imported` 与
     `attempt_history_dropped`；发生截断时另写
     `attempt_history_truncated`（含 `task_id`/`dropped`/`keep`），
     不静默丢弃。
- 自动化测试：
  - `py -m compileall -q selfdrive agent_v9.py tests`：通过。
  - `py -m unittest discover -s tests`：**142 项全部通过**
    （阶段 1.1 为 99 项，阶段 2/2.1 为 43 项）。
  - 新增 2 项回归：被合并任务已有 `attempt_history` 时，迁移后主任务保留
    该历史，同时保留当前尝试、`query_ids`、`source_ids` 与 `active` 生命周期；
    历史超过上限时按轮次保留最近 5 条，并写入可核验的截断日志。
- 隔离冒烟 v8：`runs/teacherless_smoke_20261026_v8/`（本地临时目录，不提交）。
  - `from_zero`，代码版本 `9.24.4`，指纹
    `b252df486490d245a07bbbda11ca5a7705865096d7ce61214436d8555cefab9c`，3 轮。
  - `status`：`clean=true`、`code_drift=false`、`seed_problems=[]`、
    `code_migrations=[]`、`violations={}`；8 项教师/LLM 痕迹全 0。
  - `state.research.schema_version=2`，任务/查询/来源均为 0，`stats` 全 0；
    `research.jsonl` 仅 `config_recorded`（`research-config-v2`）；
    3 轮后 0 概念/命题/关系/预测。
  - 第 2 轮辅助动作 `build_site` 在本机启动服务失败（`[WinError 2]`，
    缺少启动器），属环境限制，不影响隔离与研究账本。
  - 该冒烟为全新状态，不验证旧状态迁移与历史合并；两者由合成状态单测覆盖。
- 边界：仍不做正文提取、证据门禁、来源可信度、定律晋升与留出评估；
  不能据此宣称学习发生。

## 2026-10-26 / Phase 2.1 研究链路全链路审计补丁（v9.24.5）

- 阶段：2.1（不扩展研究功能，不进入阶段 3）
- 触发：owner 将审查范围扩大到整个阶段 2.1 研究链路（`99cc83b`），指出七个
  此前测试未覆盖的问题：候选按种类限额导致饥饿、schema 未升级、迁移合并
  语义与正常升级不一致、历史保留计数不可对账、来源成功判定可被无效 URL
  欺骗、容量修剪静默删除待验证材料、Bing 单主机“无结果”阻断另一台主机。
- 修正（一次性合并补丁）：
  1. 取消 `prediction_miss`/`claim_evidence_gap`/`relation_unverified`/
     `isolated_concept` 的每轮种类限额；候选统一按优先级与每轮新建预算竞争，
     既有任务无论预算是否耗尽都继续刷新/升级。新增 `miss_backlog`（扫描
     `edge_preds[-300:]`、上限 120），承接滑出 `edge_preds[-60:]` 的落空信号。
  2. `RESEARCH_SCHEMA_VERSION` 升到 3，新增显式 v2→v3 迁移路径；只依据
     `kind_history` 迁移轮次、查询 `created_round`/状态与当前尝试做可审计修复，
     无法重建的写 `task_migration_unrecoverable`；迁移幂等。
  3. 合并前先定最终种类；载体换种类时归档当前尝试、重置 `attempts`/`rewrites`，
     取消所有不同种类的 `planned` 查询（`cancelled` + `superseded`，ID 写入
     `kind_history`）；同种类与已执行查询、来源、材料、生命周期保留。
  4. `attempt_history` 与 `kind_history` 都按轮次排序、有界保留 5 条；
     `task_id_migrated` 分开记录双方历史、归档行、保留/丢弃，截断另写日志且
     计数可对账。
  5. 来源 URL 在解析与入账两处校验（绝对 HTTP(S)、非空、去重）；成功、
     `result_count`、`source_ids`、`pending_verification` 只由实际写入账本的
     有效来源决定；仅有无效链接记 `invalid_results`，不进入 `materials_found`。
  6. 容量修剪时，有材料的任务写入 `archived_tasks` 快照（目标/种类/查询/来源/
     验证状态/历史/归档原因，上限 40），`archived_pending()` 与 `focus()` 可发现；
     超限写 `task_archive_evicted`。
  7. `do_web_search()` 依次尝试 `cn.bing.com` 与 `www.bing.com`；单台明确
     “无结果”不再阻断另一台，成功记录使用真正返回结果的主机 URL。
- 自动化测试（临时目录副本，不触碰实时状态）：
  - `compileall selfdrive agent_v9.py tests runs/migration_check_20261026_v3/check.py`：通过。
  - `py -m unittest discover -s tests`：**163 项全部通过**
    （阶段 1.1 为 99 项，阶段 2/2.1 为 64 项）。
  - 新增/强化覆盖：四类候选多轮公平创建、预算耗尽后升级、落空补录与淘汰、
    真正 v2 状态迁移与幂等、载体换种类与多任务历史对账、单个任务超长
    `kind_history` 的截断与日志、来源 URL 五类案例、双主机回退、待验证材料
    归档与归档容量。
- 合成迁移验证：`runs/migration_check_20261026_v3/check.py` 使用临时目录构造
  v1/v2 状态，`v1`、`v2_repair`、`v2_carrier` 三案例均 `ok: true`；最终代码
  指纹 `b6c924b5a808f532fd1a33baeae1b12076b15ce6ff60227d58c3d6c298f5ad76`。
  该验证与 from_zero 冒烟相互独立，专门证明旧状态迁移。
- 隔离冒烟 v9：`runs/teacherless_smoke_20261026_v9/`（本地临时目录，不提交）。
  - `from_zero`，代码版本 `9.24.5`，指纹同前，3 轮；主动作
    `self_talk` → `tidy_notes` → `tidy_notes`，辅助动作包含
    `reflect_memory`/`rest`/`self_talk`/`metacognition`/
    `consolidate_memory`/`build_site`。
  - `status`：`clean=true`、`code_drift=false`、`seed_problems=[]`、
    `code_migrations=[]`、`violations={}`；8 项教师/LLM 痕迹全 0。
  - `state.research.schema_version=3`，任务/查询/来源/归档/落空补录均为 0，
    `stats` 全 0；`research.jsonl` 仅 `config_recorded`
    （`research-config-v3`）；3 轮后 0 概念/命题/关系/预测。
  - 第 3 轮辅助动作 `build_site` 在本机启动服务失败（`[WinError 2]`，
    缺少启动器），属环境限制，不影响隔离与研究账本。
  - 该冒烟为全新状态，不验证旧状态迁移；迁移由上面的合成验证单独覆盖。
- 边界：仍不做正文提取、证据门禁、来源可信度、定律晋升与留出评估；
  搜索材料仍全部 `unverified`；不能据此宣称学习发生。

## 2026-10-26 / Phase 3.0 网页候选验证管线（v9.25.0）

- 阶段：3.0（最小验证闭环第一小步；不是完整阶段 3）
- 假设：阶段 2 的 `pending_verification` 候选材料可以在不修改任务/来源状态、不写
  `claims`/`relations`/`laws`、不调用教师/LLM 的条件下，被安全抓取、抽取、判定，
  并写成可追溯、可幂等的候选账本。
- 实现：
  1. 新增 `selfdrive/verification.py`：`verify_task`/`verify_pending` 消费现有研究
     任务与来源账本；`sources_for_task` 支持当前索引、任务快照与共享来源的
     `query_id` 重绑定；`summarize_task`/`latest_candidates` 只读汇总。
  2. 安全抓取：仅 HTTP(S) 与 80/443；拒绝凭据、本地域名、非法 IDNA、全数字伪 IP；
     DNS 结果必须全部 `is_global`；连接固定到已校验 IP 并保留 TLS SNI；每跳重定向
     复检；5 跳上限、单跳 10 秒、总时限 30 秒、正文 2 MB；只接受 HTML/XHTML/纯文本
     与 `identity` 编码。失败类型全部显式入账。
  3. 正文抽取：HTMLParser 去脚本/导航/页脚等噪声，正文至少 20 字符、最多 20 万字符；
     不支持类型/编码、空正文、解析异常都写 `uncertain` 记录。
  4. 候选判定 `terms-cues-v1`：目标词命中、同句共现、60 字符窗口内的方向线索；
     支持与反驳冲突时判 `uncertain`；无线索不猜真值。
  5. 独立性：按最终落点域名的可注册域分组，托管平台整站合并，IP 按地址；同正文/
     同文本或无法解析域名不计独立；只有独立方向证据才 `evidence_counted=true`。
  6. 只追加 `logs/verification.jsonl`（`alive-verification/1`）：任务/查询/来源 ID、
     原始与最终 URL、重定向、正文/文本哈希、获取/事件时间、抽取状态、证据摘录、
     判定与依据、独立性、失败类型、验证器版本、配置哈希，硬编码
     `conclusion_status=candidate`、`promotion=none`。
  7. 幂等：成功按任务+来源+正文哈希+验证器版本去重；失败按任务+来源+URL+失败类型+
     验证器版本去重；验证器异常写 `verifier_exception`，不破坏其他来源记录。
  8. 命名拆分：原 `selfdrive/verification.py`（教师 `[VERDICT]` 与探针）改名为
     `selfdrive/teacher_verdict.py`；`skills.do_read_brain` 与旧测试同步更新。
- 自动化测试（代码证据）：
  - `py -m compileall -q selfdrive tests agent_v9.py semantics.py`：通过。
  - `py -m unittest discover -s tests`：**189 项全部通过**
    （阶段 1.1 为 99 项，阶段 2/2.1 为 64 项，阶段 3.0 新增 26 项）。
  - 同一套 `compileall` 与全量测试在只复制 `.py` 的纯代码临时副本中重跑，仍为
    189 项通过，确认不依赖仓库根目录的火星实时状态。
  - Phase 3.0 覆盖：端到端任务→来源→账本可追溯；重复执行幂等；空/相对/非 HTTP(S)/
    私有/localhost URL；重定向复检与落点分组；网络失败/超时/抓取器异常/正文截断/
    读取截止；不支持类型/编码/空正文；同域、跨域同正文、无法解析来源的独立性；
    支持+反驳冲突；共享来源 query_id 重绑定；当前与归档待验证任务；验证器异常安全。
- 验证管线冒烟（隔离证据，`runs/phase3_verification_20261026/verify/`）：
  - 代码指纹 `a311f004e44b50aa5c5258892693c044ef86312aa9c1780905fc941984573ad4`；
    验证器配置哈希 `af82789a82e6701b60e271cc61b01475208a67276d458b23b697812655ad6ae2`。
  - 合成预测落空任务经真实 `research.record_execution` 入账两条来源；假 HTML 抓取后
    账本 2 行、支持/反驳各 1 个独立组，结论 `candidate_conflict`。
  - 22 项检查全 true：状态 JSON 不变、任务仍 `pending_verification`、来源仍
    `unverified`、无 `claims`/`evidence`/`relations`/`laws`、无教师/LLM 痕迹、
    重复执行不追加、非法 URL 显式失败且不晋升。
  - 真实网络观察 `https://example.com/`：HTTP 200、正文 577 字节、标题
    `Example Domain`、文本 156 字符；仅作环境观察，不参与通过/失败。
- Teacherless 冒烟（隔离证据，`runs/phase3_verification_20261026/teacherless/`）：
  - `from_zero` 新实验 `TL-PHASE3-20261026`，代码版本 `9.25.0`，指纹同上，3 轮。
  - `status`：`clean=true`、`code_drift=false`、`seed_problems=[]`、
    `code_migrations=[]`、`violations={}`、`blocked_channel_log_counts={}`；
    8 项教师/LLM 痕迹全 0。
  - 第 1/3 轮辅助动作 `build_site` 在本机启动服务失败（`[WinError 2]`，
    缺少启动器），属环境限制，不影响隔离边界与研究/验证账本。
  - 该冒烟只证明教师/LLM 通道仍关闭、验证模块未污染隔离状态，不证明学习发生。
- 证据报告：`runs/phase3_verification_20261026/test_report.json` 与 `report.json`
  （基线提交 `2500c4e`、代码指纹、配置快照、测试、两段冒烟；`runs/` 不提交）。
- 边界：没有写 `claims`/`evidence`/`relations`/`laws`，没有改变任务/来源状态，
  没有接入 `main.py`，没有自动晋升、回滚或留出评估。`candidate_support` 只是候选；
  本阶段只证明验证管线可运行，不证明系统学会知识或发生进化。

## 2026-10-27 / Phase 3.0 复审与预注册草案（v9.25.0，无代码变更）

- 触发：owner 提供 `NEXT_TASK_PROMPT.md`，要求先执行步骤 0（复审与基线复验）并给出
  步骤 1 预注册草案；若没有 Phase 3.0 owner 复审通过记录，完成审查和草案后停止。
- 复审状态：**未发现 owner 对 Phase 3.0 的复审通过记录**。`PHASE3_VERIFICATION.md`
  写明“等待 owner 复审”，`EXPERIMENT_LOG.md` 的下一门槛仍要求 owner 复审，
  `MASTER_PROMPT.md` 只记录“已实现”。按门禁停止：不实现自动知识晋升，不接主循环。
- 基线复验（代码证据）：
  - Python 3.14.7；`py -m compileall -q selfdrive tests agent_v9.py semantics.py` 通过。
  - `py -m unittest discover -s tests`：**189 项全部通过**，2.152s。
  - `py -m unittest discover -s tests -p "test_verification_phase3.py"`：**26 项通过**，0.244s。
  - 代码指纹 `a311f004e44b50aa5c5258892693c044ef86312aa9c1780905fc941984573ad4`。
- 隔离冒烟复验（隔离证据，`runs/phase3_review_20261027/verify/`）：
  - 全新临时副本，22/22 项检查 true；配置哈希
    `af82789a82e6701b60e271cc61b01475208a67276d458b23b697812655ad6ae2`。
  - 账本 2 行，`candidate_conflict`（支持 1/反驳 1）；重复执行不追加；
    非法 URL 显式 `non_public_address` 失败且不晋升。
  - 可选真实网络观察 `https://example.com/` 在本机返回 `tls_error`
    （`SSLCertVerificationError`，本地 CA 缺失）；失败关闭，不参与通过判定。
- 四个潜在断点（详见 `PHASE3_REVIEW.md`）：
  1. 网页缓存仍可进入 `learn_from_text`：`workspace/web` 由搜索写入未验证摘要
     （`skills.py:832-851`），`scan_workspace` 扫描 WEB（`skills.py:46-58`），
     `do_explore_file` 可调用 `learn_from_text`（`skills.py:191,208-211`）；
     `learn_from_text` 会改概念/关系/规则（`learning.py:197-267`）。Teacherless
     未禁用 `explore_file`/`web_search`（`teacherless.py:33-35`；`main.py:69-74`）。
     这阻塞“现有主循环无教师学习”的表述；不阻塞显式调度器的封闭算术实验。
  2. LLM 提案：`save_proposals` 只写 `llm_proposals`（`llm_bridge.py:266-269`），
     但普通模式 `do_self_tune` 会直接写 `brain/` 配置（`skills.py:1258-1298`）。
     Teacherless 下 `llm_consult` 硬禁用；不得写“LLM 永远只提案”。
  3. Semantics/KB 仍是旁路：`main.py:25` 构造、`main.py:195` 只 `save()`；
     无 `learn`/`encode`/`closest` 调用；`kb_recall` 无调用者；Teacherless 下
     `kb_remember` 返回 `False`。不得计为有效认知能力。
  4. 动作失败可能记成功：`build_site` 启动服务失败仍返回 `True`（`skills.py:908`）；
     LLM 自改路径只要有提案就返回 `True`（`skills.py:1294-1298`）；`ok` 驱动
     能量/情绪/结算（`main.py:156,172,192`）。不得用动作成功率/情绪当学习证据。
- 其他阻塞“可撤销更新”的问题：
  - `claims.add_evidence` 按独立组去重，同组后来的相反极性只增加 `repeat_count`
    （`claims.py:40-83`）；算术组是 `probe:<expr>`（`claims.py:106`），反例会丢失。
  - `record_transform_verdict` 直接改写 claim 状态/置信度（`claims.py:101-112`），
    没有前后快照与回滚。
  - `conceptcard.on_transform_verdict` 只被教师路径调用（`teacher_verdict.py:57,70`），
    普通无教师路径不触发 L001 毕业。
  - 探针仅支持中文数词 1..10、单个数词结果；和超过 10 返回 `None`（未知），
    不是反驳（`probes.py`）。`transform_compose` 不是通用加法器（`learning.py:412-458`）。
  - Phase 3.0 无调度器/限流，符合当前授权；验证账本路径依赖 `config.BASE`，
    实验必须隔离。
- 本轮文档：
  - 新增 `PHASE3_REVIEW.md`（复审报告）与 `PREREG_ARITHMETIC.md`（预注册草案，
    **DRAFT，未获 owner 批准**）。
  - 更新 `PHASE3_VERIFICATION.md` 状态/复验/下一门槛、`MASTER_PROMPT.md` 当前进度、
    `README.md` 状态、本日志。
- 边界：**无代码变更**；未运行正式实验；未写 `claims`/`evidence`/`relations`/`laws`；
  未接主循环；不声称学习增益或进化。

## 下一门槛

1. owner 复审 Phase 3.0 候选验证管线与 `PHASE3_REVIEW.md`，明确批准或要求修正。
2. owner 确认 `PREREG_ARITHMETIC.md` 草案；在批准前不实现候选规则消除、不写自动
   知识更新、不接主循环。
3. 若批准：先建独立评估器与基线（步骤 2），再做最小验证器注册表（步骤 3），再做
   可撤销知识更新（步骤 4）；每一步停下等 owner 复审，不自动进入下一模块。
4. 正式开跑前冻结代码版本；升级只能 `accept-code` 留下迁移记录，重大算法变更新建实验。
5. 在留出任务评估与可撤销知识更新建立之前，`candidate_support` 只是候选；
   任何“学会/进化”表述都不成立。
