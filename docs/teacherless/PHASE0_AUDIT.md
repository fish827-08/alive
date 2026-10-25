# Phase 0 代码审计与 Phase 1 隔离基线报告

- 日期：2026-10-25
- 范围：仓库 `D:\Project\alive` 的真实代码与测试
- 结论：阶段 1 的“教师/LLM 零调用 + 独立初始化 + 种子账本”已实现并通过测试与两轮冒烟；
  阶段 2 及以后未实现，不得把本报告当作“自主学习能力已提升”的证据。

## 1. 审计结论摘要

1. 当前系统的核心决策链确实是无 LLM 的符号/统计代码；LLM 只是一个可关闭的提案桥。
2. 教师依赖不止 `ask_brain`。真正危险的是**历史状态继承**：
   `state.json`、`brain/inbox.md`、`brain/outbox/`、教师模板、`compiled_rules`、
   `infer_log` 中的教师裁决、`semantics/` 指纹、共享向量库、外部目录扫描和兄弟信箱。
3. 只在决策层把教师技能降权不够。教师历史仍会通过工作记忆目标、未读回答计数、
   模板回填和共享记忆服务影响行为。Phase 1 因此把检查放在**初始化、启动和执行入口**。
4. 关闭教师通信不等于从零开始。新的 `selfdrive/teacherless.py` 会拒绝在任何已有
   `state.json`、`brain/`、`workspace/`、`logs/`、`semantics/` 载荷的目录上初始化；
   `from_zero` 只允许代码级先验，并把代码指纹和内置先验写进 manifest。
5. 仓库根目录是火星的实时运行目录。本轮没有修改或暂存它的 `state.json`、`brain/`、
   `logs/`、`workspace/`；所有冒烟都在 gitignored 的 `runs/teacherless_smoke_20261025/` 中进行。

## 2. 审计方法与边界

- 逐文件阅读 `selfdrive/` 包、`semantics.py`、`agent_v9.py`、测试与 README。
- 追踪真实调用关系，而不是依据设计文档推断。
- 运行现有测试与新增测试；不带网络依赖，不连接云端服务器。
- 没有对辉月、moon、火星的运行状态做迁移、清理或“修复”。
- 没有把仓库根目录的实时状态复制进 Teacherless 实验。

## 3. 当前真实执行链

```text
agent_v9.py
  └─ selfdrive.main.main()
       ├─ [teacherless] teacherless.verify_clean_start()
       ├─ templates.ensure_builtin_templates()
       ├─ state.load_state()
       ├─ [teacherless] teacherless.stamp_state() + assert_state_clean()
       └─ run_round(st)
            ├─ harvest_think()                    # LLM 后台思考收割
            ├─ templates.check_template_retirement()
            ├─ conceptcard.situation_match()      # 概念卡处境匹配
            ├─ skills.scan_workspace()
            ├─ working_memory.build()
            ├─ decision.pick_actions()            # 主动作 + 辅动作
            ├─ decision.evaluate_candidates()
            ├─ working_memory.attach_decision()
            ├─ outcomes.begin()                   # 行动前预期先落盘
            ├─ save_state()
            ├─ 执行主动作 handler
            ├─ 执行辅动作 handler
            ├─ apply_energy()
            ├─ update_emotions()
            ├─ rules_compiler.apply_round_rules()
            ├─ 结算感受队列
            ├─ outcomes.settle()                  # Brier/信用归因
            ├─ log_event()
            ├─ save_state() + SEM.save()
            └─ [teacherless] teacherless.assert_state_clean()
```

相关位置：

- 主循环：`selfdrive/main.py:41`（`run_round`）、`selfdrive/main.py:194`（`main`）
- 决策门控：`selfdrive/decision.py:21`（`legacy_weights`）、`:164`（`evaluate_candidates`）、
  `:205`（`choose_action`）、`:243`（`pick_actions`）
- 工作记忆：`selfdrive/working_memory.py:6`
- 教师入口：`selfdrive/skills.py:316`（`do_ask_brain`）、`:480`（`do_read_brain`）、
  `:298`（`_write_question`）
- 外部扫描：`selfdrive/skills.py:58`（`scan_wider`）、`:216`（`do_explore_wider`）
- LLM：`selfdrive/llm_bridge.py`
- 共享向量库：`selfdrive/kb.py`
- 模板回填：`selfdrive/templates.py:133`
- 概念卡未读回答：`selfdrive/conceptcard.py:75`
- Teacherless 模块：`selfdrive/teacherless.py:43`（`is_enabled`）、`:170`（`init_experiment`）、
  `:316`（`verify_clean_start`）、`:342`（`stamp_state`）、`:371`（`assert_state_clean`）、
  `:437`（`status`）

## 4. 教师 / LLM / 外部通道入口表

| 入口 | 位置 | 实际行为 | 风险 | Phase 1 处理 |
|---|---|---|---|---|
| `ask_brain` | `skills.py:316` | 写 `brain/inbox.md`，增 `brain_questions`，更新查重窗口 | 教师反馈进入实验 | 执行层硬拒绝；决策权重清零 |
| `read_brain` | `skills.py:480` | 读 `brain/outbox/*.md`；提取模板、编译 `[RULE]`、应用 `VERDICT`、`learn_from_text` | 教师知识直接变成本实验知识 | 执行层硬拒绝；不读 outbox |
| `_write_question` | `skills.py:298` | 追加 inbox 并记问题 | 被 `ask_brain` 调用 | 只从被禁用的 `ask_brain` 到达 |
| 未读回答计数 | `decision.py:58`、`working_memory.py:9`、`conceptcard.py:89` | 影响 read/ask 权重、工作记忆目标、概念卡激活 | 历史 outbox 会改变当前行为 | Teacherless 下不扫描 outbox |
| 模板回填 | `templates.py:142` | `asked_texts` 为空时从 `brain/inbox.md` 回填 | 教师历史问题变成查重窗口 | Teacherless 下直接返回空 |
| `explore_wider` | `skills.py:216` | 扫描 `WIDER_ROOTS`（`/home/qingwu`、`/usr/share/doc`） | 可读到 moon、教师资料、共享信件 | 执行层硬拒绝；`WIDER_ROOTS` 置空 |
| `sibling_letter` | `skills.py:1223` | 读/写 `shared_letters/` | 兄弟信可能夹带教师转述；身份不独立 | 执行层硬拒绝；`SIBLING_NAME` 置空 |
| `llm_consult` | `skills.py:1099` | 调 Ollama 提案接口 | LLM 参与候选生成 | 执行层硬拒绝 |
| LLM 思考皮层 | `llm_bridge.py` | `think` / `think_async` / `harvest_think` | 继承的 `think_inbox` 或新调用进入提案 | 所有入口返回空；继承队列丢弃 |
| 共享向量库 | `kb.py` | 读写 `127.0.0.1:8100`，`agent_id` 固定 `selfdrive` | 教师历史与其它 agent 的记忆混用 | 读写都硬禁用 |
| `self_tune` 的 LLM 分支 | `skills.py:1124` | LLM 可用时提案改 emotions/ask_config | LLM 间接改变行为 | `LLM_ENABLED` 在 Teacherless 下强制 False |
| `self_talk` 的 LLM 分支 | `skills.py:1324` | `think_available` 时派发异步思考 | LLM 间接进入提案 | `think_available` 在 Teacherless 下 False |
| `web_search` | `skills.py:699` | Bing 抓取 → `workspace/web/*.md` | 不是教师/LLM，但搜索结果可含未核验主张；会写共享 kb | Phase 1 允许作为“观察”，但不写共享 kb；冒烟中显式禁用 |
| `build_site` / `refresh_site` | `skills.py` | 生成/启动本地网页；`refresh_site` 调 `python3 -m http.server` | 无教师风险；Windows 下 `python3` 可能不存在 | 不禁用；平台问题见第 9 节 |

## 5. 隐性继承路径与风险表

| 路径 | 为什么会继承教师 | Phase 1 处理 | 残留风险 |
|---|---|---|---|
| 旧 `state.json` | 含概念、关系、`compiled_rules`、`brain_read`、`infer_log` 教师裁决、LLM 提案 | `init_experiment` 拒绝任何已有 `state.json` | 若人工在初始化后复制 state，`verify_clean_start` 会因 marker/基线不匹配拒绝 |
| `brain/inbox.md`、`brain/outbox/` | 教师问答原文 | 初始化拒绝；运行期不读不写 | 初始化后人工投放 outbox 不会被使用（read_brain 硬禁用），但 `contamination_markers` 会报告非空 outbox |
| `brain/templates/*.txt` | 教师回答里提取的模板 | 初始化拒绝已有模板；`ensure_builtin_templates` 只写内置两件 | 初始化后人工复制教师模板不会被使用（`ask_brain` 硬禁用），但当前不会单独识别模板来源 |
| `compiled_rules` / `skill_multipliers` | 教师 `[RULE]` 编译结果 | `from_zero` 基线计数必须为 0；运行期无 `read_brain` | 若人工在初始化后写入 state 规则，基线检查会拒绝启动/轮末失败 |
| `infer_log.teacher_verdict` | 教师判卷证据 | `artifact_counts` 把教师裁决计入基线；from_zero 必须为 0 | 无 |
| `llm_proposals` / `llm_budget` / `think_budget` / `think_inbox` | 旧 LLM 提案与预算 | 初始化拒绝旧 state；运行期 LLM 全禁；`harvest_think` 丢弃继承队列 | 无 |
| `letters_read` / `wider_files` | 兄弟信与外部目录扫描结果 | 初始化拒绝旧 state；两个技能硬禁用 | 无 |
| `semantics/ae.json`、`memory.jsonl` | 旧文本指纹，可能来自教师回答 | 初始化拒绝已有 `semantics/` 载荷 | 初始化后人工复制 semantics 不会被自动识别；正式实验前应使用全新目录 |
| 共享向量库 | 教师历史与其它 agent 的记忆共用服务 | 读写都禁用 | 无 |
| `ask_config.json` 的 `self_tuned` 段 | `self_tune` 持久化调参 | 初始化拒绝已有 brain 载荷；运行期允许自调 | 自调是本实验自己的行为，不是教师；但会改变后续行为，评估时需记录 |
| 代码本身 | 内置模板、概念卡、L001、权重、情绪默认值 | 作为 `base_code` 先验写入 manifest；记录代码指纹 | 启动后修改代码不会改变已记录指纹；`status` 报告 `code_drift`，正式实验应重新初始化 |
| 网页自食缓存 | 旧 `workspace/web/web-*.md` | 初始化拒绝已有 workspace 载荷 | 运行期自己产生的 web 缓存允许，但当前搜索仍是无目标的关键词搜索（Phase 2 才做定向规划） |

## 6. 可复用模块清单

| 模块 | 现状 | Phase 1 用法 | 后续阶段 |
|---|---|---|---|
| `state.py` | 原子写、损坏拒绝、默认状态 | 复用 | 增加来源字段时保持兼容 |
| `claims.py` | 命题/证据门禁/来源可靠性 | 保留 | Phase 3 扩展证据状态与独立证据计数 |
| `probes.py` | 封闭中文数词加法探针 | 保留（代码级验证器） | 扩展验证器注册表 |
| `verification.py` | 教师意见与探针裁决汇合 | 保留但教师入口被禁用 | Phase 3 改造成验证器注册表 |
| `predictions.py` | 无边概念对下注、超时结算 | 保留 | Phase 4 升级为任务级预测 |
| `outcomes.py` | 行动前预期、实际结果、Brier | 保留 | Phase 4 接入评估 |
| `learning.py` | 概念晋升、关系、变换、碎片合并 | 保留 | Phase 2/3 接收研究任务产出的文本与证据 |
| `conceptcard.py` | 七面概念卡、Laws | 保留；Teacherless 下不读 outbox | Phase 3 接验证器 |
| `working_memory.py` | 目标/缺口/证据/候选 | 保留；Teacherless 下目标不含 `read_teacher_response` | Phase 2 接研究任务队列 |
| `decision.py` | 多门控权重 + 候选后果评估 | 保留；新增第 ⑯ 门清零禁用通道 | Phase 4 接策略评估 |
| `skills.py` | 16 个技能实现 | 保留；5 个通道硬禁用 | Phase 2 新增研究/搜索技能 |
| `kb.py` | 共享向量服务客户端 | Teacherless 下禁用 | M1 可选语义检索时再评估独立实例 |
| `llm_bridge.py` | 可关闭 LLM 提案桥 | Teacherless 下全禁 | M2 仅在明确对照实验中开启 |
| `semantics.py` | 本地自编码器/记忆指纹 | 保留独立 `semantics/` | 不跨实验共享 |

## 7. Phase 1 实际实现

### 7.1 新模块 `selfdrive/teacherless.py`

提供：

- `is_enabled()`：读取 `config.TEACHERLESS`。
- `init_experiment()`：在独立目录建立 manifest 与目录骨架；拒绝已有知识载荷；
  拒绝重复初始化；记录种子来源。
- `load_manifest()`：manifest 损坏或格式不合法时拒绝静默重建。
- `verify_clean_start()`：启动前要求 manifest；state 必须属于同一实验；
  `from_zero` 还要求无污染笔记/outbox 与被禁通道事件。
- `stamp_state()`：把 `experiment_id`、`seed_policy`、种子哈希、基线计数写入 state。
- `assert_state_clean()`：基线之后任何教师/LLM 计数增长都报错。
- `assert_mode_matches_state()`：standard 模式拒绝启动 Teacherless 状态。
- `artifact_counts()`：统计 8 类历史痕迹。
- `contamination_markers()` / `log_channel_counts()`：扫描笔记标记与事件日志。
- `audit()`：追加 `logs/teacherless_audit.jsonl`。
- `status()`：输出 manifest、基线、当前计数、违规、污染标记、代码漂移。
- CLI：`python -m selfdrive.teacherless init|status`。

manifest 关键字段：

```json
{
  "format": "alive-teacherless-manifest/1",
  "experiment_id": "TL-...",
  "mode": "teacherless",
  "code_version": "9.23",
  "code_sha256": "...",
  "seed_policy": "from_zero | declared | teacher_history_seeded",
  "clean_from_zero": true,
  "seed_items": [
    {"source_type": "base_code", "kind": "runtime_code", "paths": ["..."], "sha256": "..."},
    {"source_type": "base_code", "kind": "builtin_priors", "description": "内置模板/概念卡/L001/权重/情绪默认值"}
  ],
  "seed_items_sha256": "...",
  "channels_disabled": ["ask_brain", "explore_wider", "llm_consult", "read_brain", "sibling_letter", "shared_kb"],
  "llm_enabled": false,
  "web_search": "observation_allowed"
}
```

### 7.2 模式开关

`selfdrive/config.py`：

- 新增 `SELFDRIVE_MODE`，只接受 `standard` / `teacherless`。
- `TEACHERLESS` 为真时：
  - `LLM_ENABLED = False`（即使 `SELFDRIVE_LLM=1`）。
  - `SIBLING_NAME = ""`（即使 `SELFDRIVE_SIBLING` 已设置）。
  - `WIDER_ROOTS = []`。
- 默认 `standard`，不改变辉月、moon、火星现有启动方式。

### 7.3 执行层硬门禁

| 文件 | 改动 |
|---|---|
| `main.py` | Teacherless 启动前 `verify_clean_start`；加载 state 后 `stamp_state`/`assert_state_clean`；每轮结束再验；被禁动作在执行前过滤；standard 模式拒绝 Teacherless state |
| `skills.py` | `do_ask_brain`、`do_read_brain`、`do_explore_wider`、`do_llm_consult`、`do_sibling_letter` 首行硬返回；写 `teacherless_audit.jsonl` |
| `decision.py` | 第 ⑯ 门：Teacherless 下禁用技能权重清零；Teacherless 下不扫描 outbox |
| `working_memory.py` | 不扫描 outbox；目标不含 `read_teacher_response`；`available_skills` 排除禁用技能；写 `mode=teacherless` |
| `llm_bridge.py` | `available`/`think_available`/`consult`/`save_proposals`/`harvest_think` 全部硬门禁；继承的 `think_inbox` 直接丢弃 |
| `kb.py` | `kb_remember`/`kb_recall` 硬禁用 |
| `templates.py` | `ensure_asked_texts` 不再从 `brain/inbox.md` 回填 |
| `conceptcard.py` | 不再因 outbox 未读回答点亮“回答”卡 |
| `config.py` | 强制关闭 LLM/兄弟信箱/外部扫描 |

### 7.4 本轮修改文件

新增：

- `selfdrive/teacherless.py`
- `tests/test_teacherless.py`
- `docs/teacherless/MASTER_PROMPT.md`
- `docs/teacherless/PHASE0_AUDIT.md`
- `docs/teacherless/EXPERIMENT_LOG.md`

修改：

- `selfdrive/config.py`
- `selfdrive/main.py`
- `selfdrive/skills.py`
- `selfdrive/decision.py`
- `selfdrive/working_memory.py`
- `selfdrive/llm_bridge.py`
- `selfdrive/kb.py`
- `selfdrive/templates.py`
- `selfdrive/conceptcard.py`
- `tests/test_state_memory_decision.py`（修正 v9.23.2 后失效的 `pick_actions` 替身目标）
- `README.md`

未修改：仓库根目录的 `state.json`、`brain/`、`logs/`、`workspace/`、`moon/`、
`docs/` 既有设计稿（除新增 teacherless 文档）。未新增第三方依赖。

## 8. 测试与验收

### 8.1 自动化测试

- `py -m compileall -q selfdrive agent_v9.py tests`：通过。
- `py -m unittest discover -s tests`：**80 项全部通过**。
- 其中 `tests/test_teacherless.py`：**42 项**，覆盖：
  - 干净目录初始化、重复初始化拒绝、state/inbox/notes/logs/semantics 载荷拒绝；
  - `from_zero` / `declared` / `teacher_history_seeded` 三种种子策略；
  - manifest 缺失、实验 ID 不一致、种子哈希不一致、基线增长、污染笔记、
    被禁通道事件、standard 模式误启动；
  - 5 个禁用技能的“零文件、零网络、零调用”；
  - LLM 环境变量强开仍为 0 调用；
  - 共享向量库禁用；
  - 工作记忆不读 outbox、目标不含教师回答；
  - 决策权重清零；
  - 模板不回填教师 inbox；
  - 概念卡不点亮“回答”；
  - standard 模式原有行为不受影响；
  - `SELFDRIVE_MODE=teacherless` 对 LLM/兄弟/外部扫描的强制关闭。

### 8.2 对照总提示词第十四节“必须提供的测试”

| # | 要求 | Phase 1 状态 |
|---|---|---|
| 1 | 教师入口禁用 | 已测：`test_ask_brain_writes_nothing`、`test_read_brain_reads_no_teacher_answer` |
| 2 | LLM 禁用 | 已测：`test_llm_gates_even_if_env_enabled`、`test_llm_consult_never_calls_model` |
| 3 | 教师历史数据隔离 | 已测：初始化拒绝旧载荷、marker/基线校验、继承 `think_inbox` 丢弃 |
| 4 | 知识来源追踪 | 已实现：manifest `seed_items` + 代码指纹 + state marker；外部种子文件的实际哈希尚未实现 |
| 5 | 同源证据去重 | 未实现（Phase 2 `evidence_store`） |
| 6 | 宽泛搜索/重复查询抑制 | 未实现（Phase 2 `search_planner`） |
| 7 | 无结果/超时/解析失败 | 未实现专项测试；现有 `web_search` 有异常回退，但 Phase 1 未验证 |
| 8 | 不能验证的主张保持未知 | 现有 claims/verification 有部分门禁；Phase 1 未新增端到端测试（Phase 3） |
| 9 | 算术验证器正确性 | 现有测试通过（`probes`/`verification` 相关用例） |
| 10 | 预测先记录再结算 | 现有测试通过（`outcomes` + 主循环测试） |
| 11 | 反例降信/撤销规则 | 现有测试通过（`verification`/`claims` 相关用例） |
| 12 | 留出任务证明学习改进 | 未实现（Phase 4） |
| 13 | 策略修改失败可回滚 | 未实现（Phase 4） |
| 14 | 原版辉月/moon/火星状态不被修改 | 本轮通过“只改代码/文档/测试 + 冒烟在 runs/”保证；尚无自动哈希校验测试 |

**阶段 1 验收口径：只对第 1–4、14 项负责。** 第 5–8、12–13 项属于后续阶段；
第 9–11 项是既有能力，不是本轮新增成果。

## 9. 隔离冒烟证据

目录：`runs/teacherless_smoke_20261025_v2/`（`.gitignore` 已忽略 `runs/`）

初始化：

```bash
py -m selfdrive.teacherless init --experiment TL-SMOKE-20261025-V2 --name 无师
```

结果：`seed_policy=from_zero`、`clean_from_zero=true`、`llm_enabled=false`、
`channels_disabled` 包含 5 个技能 + `shared_kb`。

运行（故意把 LLM/兄弟环境变量开到最大）：

```bash
SELFDRIVE_MODE=teacherless SELFDRIVE_LLM=1 SELFDRIVE_SIBLING=moon py -u agent_v9.py --once
```

两轮结果：

- 轮 1：主动作 `self_talk`；辅动作 `build_site`、`reflect_memory`。
- 轮 2：主动作 `tidy_notes`；辅动作 `rest`、`self_talk`。
- `logs/events.jsonl` 共 6 条，`ask_brain`/`read_brain`/`llm_consult`/
  `sibling_letter`/`explore_wider` 事件数均为 0。
- `teacherless status`：
  - `artifact_counts` 8 项全部为 0；
  - `baseline_counts` 8 项全部为 0；
  - `violations={}`、`contamination_markers={}`、`blocked_channel_log_counts={}`；
  - `code_drift=false`（manifest 代码指纹与运行代码一致）；
  - `clean=true`。
- `state.json`：`rounds=2`；`teacherless` marker 存在且实验 ID/种子哈希与 manifest 一致；
  `llm_proposals=0`；`brain_questions=0`；`brain_read=0`；
  `working_memory.goal=learn_and_verify`；`available_skills=11`（16 个技能减 5 个禁用）。
- 文件落点：只有 `brain/ask_config.json`、`brain/emotions.json`、
  `brain/templates/seed-*.txt`、`logs/*`、`workspace/notes/index.md`、
  `workspace/web/index.html`；没有生成 `brain/inbox.md` 或 `brain/outbox/`。
- 用 `SELFDRIVE_MODE=standard` 启动同一目录：进程以 `RuntimeError` 退出（exit=1），
  状态仍是 `rounds=2`、`clean=true`。

**不能据此宣称学习进步**：两轮后 `concepts=0`、`claims=0`、`relations=0`。
冒烟目录没有训练资料，Phase 1 验证的是隔离和可运行性，不是理解能力。
`refresh_site` 在早先的同构冒烟中曾在 Windows 上报 `[WinError 2]`（找不到 `python3`），
属于既有平台问题，不构成教师/LLM 泄漏。

## 10. 已知限制与未实现

1. **无训练口粮的 from_zero 基线**：当前冒烟没有任何 archive 资料，因此不能展示学习。
   Phase 1 只证明“能隔离运行”。
2. **外部种子文件只记录元数据**：`init_experiment(seed_entries=...)` 可以声明
   `training_material` / `teacher_history` 等来源，但没有把实际文件哈希写进 manifest。
   正式喂资料前需要补“声明 + 哈希 + 落盘”流程（Phase 2 门槛）。
3. **没有研究任务、定向搜索、证据库、验证器注册表、世界模型、策略优化器**。
   总提示词中的 `research.py`、`search_planner.py`、`evidence_store.py` 等尚未创建。
4. **没有留出任务评估**：无法回答“它是否学会”，只能回答“它是否零调用教师/LLM”。
5. **web_search 仍是无目标关键词搜索**：Phase 1 允许它作为观察通道，但未做查询预算、
   去重、来源核验；冒烟中直接禁用。
6. **semantics 目录的初始化后人工复制无法自动识别**：初始化拒绝旧载荷是主防线；
   正式实验必须使用全新目录，不能手工搬运。
7. **代码漂移只报告不阻断**：`status` 有 `code_drift`，启动审计记录该值；开发期允许
   代码变化，正式实验应在代码冻结后重新初始化。
8. **`build_site` / `refresh_site` 的 Windows 兼容性**：`refresh_site` 调用 `python3`，
   Windows 无该命令时失败；不影响隔离，但影响行动成功率统计。
9. **长跑未验证**：只跑了 `--once` 两轮，没有验证 180 秒常驻循环、崩溃恢复、
   跨日预算与日志轮转。
10. **原版学生状态没有自动哈希校验测试**：本轮靠操作边界保证未修改；
    后续可加只读哈希快照测试，但不能把 2MB 实时 state 纳入普通单测。

## 11. Phase 2 进入门槛

只有满足以下条件才进入阶段 2：

1. owner 检查并认可本报告的阶段 1 范围与证据口径。
2. 明确第一个正式实验的种子策略：
   - `from_zero`：全新目录、无任何训练资料，只验证隔离与自发行为；
   - `declared`：允许公开训练资料，但必须先实现文件哈希与来源登记；
   - `teacher_history_seeded`：只能作为“停止教学后的自主延续”对照，不得称为从零。
3. 先定义研究任务 schema、证据 schema 和留出任务，再写搜索规划器；
   不得先堆搜索代码再补评估。
4. 为 `web_search` 加请求预算、超时、重复查询抑制和来源记录，并补失败路径测试。
5. 任何“学习提升”声明必须来自留出任务或受控实验，不能用轮次/概念数/情绪值代替。
