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

## 下一门槛

1. owner 检查 Phase 1.1 修正（代码版本硬门禁、L001 溯源、种子账本）与文档，
   确认是否进入阶段 2。
2. 进入阶段 2 前必须先确定：
   - 正式实验的种子策略（`from_zero` / `declared` / `teacher_history_seeded`）；
   - 外部训练资料通过 `seed_entries` 登记（path/哈希/大小/导入方式）；
   - 研究任务、证据、留出任务的 schema；
   - 运行期网络资料的 URL/获取时间/原文/哈希/独立来源记录方式。
3. 正式开跑前冻结代码版本；升级只能 `accept-code` 留下迁移记录，
   重大算法变更新建实验。
4. 在留出任务评估建立之前，任何“学会/进化”表述都不成立。
