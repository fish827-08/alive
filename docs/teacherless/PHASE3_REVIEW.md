# Phase 3.0 复审报告（步骤 0）

- 日期：2026-10-27
- 基线：`a20bf5b` / v9.25.0
- 复审者：AI 开发会话（**不是 owner**）
- 状态：**审查完成；未发现 owner 对 Phase 3.0 的复审通过记录。按 `NEXT_TASK_PROMPT.md`
  门禁第 1 条与第十一节，本轮到此停止，不实现自动知识晋升，不接主循环。**

## 0. 结论

1. Phase 3.0 的代码、测试、冒烟与文档在受审范围内一致；没有发现会破坏候选验证管线
   本身的实现缺陷。
2. 但仓库中**没有** owner 明确批准 Phase 3.0 的记录：`PHASE3_VERIFICATION.md` 写着
   “等待 owner 复审”，`EXPERIMENT_LOG.md` 的“下一门槛”仍要求 owner 复审，
   `MASTER_PROMPT.md` 只记录“已实现”，没有批准结论。
3. 因此本轮只交付本报告与 `PREREG_ARITHMETIC.md` 草案；**没有**修改任何学习/晋升逻辑，
   **没有**接入 `main.py`，**没有**运行正式实验，**不能**声称观察到学习增益。
4. 审查发现四个潜在断点。它们不阻塞“封闭算术实验 + 显式调度器”这一最小闭环，
   但其中“网页缓存进入 `learn_from_text`”会阻塞任何“现有主循环在无教师条件下学到知识”
   的表述；现有 `claims.add_evidence` 的证据组去重与直接改写状态会阻塞“可撤销知识更新”
   这一步，需要在新实验中重新设计，而不是复用。

## 1. 复审依据与基线复验

### 1.1 Git 与文件状态

- HEAD：`a20bf5b`（v9.25.0 阶段 3.0 候选验证管线）。
- `git diff --stat -- selfdrive tests docs/teacherless`：空；本轮开始时受审代码与文档
  没有未提交改动。
- 仓库存在火星实时运行产生的未提交文件（`state.json`、`brain/`、`logs/`、`workspace/`
  等），本轮未读取为种子、未修改、未暂存。
- 本轮开始时唯一未跟踪的教师less文档是 owner 提供的 `docs/teacherless/NEXT_TASK_PROMPT.md`。

### 1.2 本次实际运行的测试（代码证据）

| 命令 | 结果 |
|---|---|
| `py -V` | Python 3.14.7 |
| `py -m compileall -q selfdrive tests agent_v9.py semantics.py` | 通过（退出码 0） |
| `py -m unittest discover -s tests` | **189 项全部通过**，2.152s |
| `py -m unittest discover -s tests -p "test_verification_phase3.py"` | **26 项全部通过**，0.244s |
| `py -c "from selfdrive.teacherless import code_fingerprint; ..."` | `a311f004e44b50aa5c5258892693c044ef86312aa9c1780905fc941984573ad4` |

这些是**代码证据**，只证明当前代码通过既有测试，不证明学习发生。

### 1.3 隔离冒烟复验（隔离证据）

在全新临时副本 `runs/phase3_review_20261027/verify/` 中重跑阶段 3.0 冒烟：

- 代码指纹：`a311f004e44b50aa5c5258892693c044ef86312aa9c1780905fc941984573ad4`。
- 验证器配置哈希：`af82789a82e6701b60e271cc61b01475208a67276d458b23b697812655ad6ae2`。
- **22/22 项检查为 true**，`all_checks_ok=true`。
- 账本 2 行，结论 `candidate_conflict`（独立支持 1、独立反驳 1），
  `conclusion_status=candidate`、`promotion=none`。
- 幂等：第一次追加 2 行，第二次追加 0 行，账本仍 2 行。
- 非法 URL：`non_public_address`，显式失败，不晋升。
- 可选真实网络观察 `https://example.com/` 在本机返回 `tls_error`
  （`SSLCertVerificationError: unable to get local issuer certificate`）。这是失败的
  **失败关闭**记录，不参与冒烟通过判定；2026-10-26 的成功观察是历史环境结果，
  不能据此声称当前机器可稳定访问公网。

这段是**隔离运行证据**，只证明候选验证管线可运行且未污染隔离状态，不证明学习。

### 1.4 未发现 owner 批准记录

搜索 `docs/teacherless/` 中与 Phase 3.0 复审/批准相关的文字，只看到：

- `PHASE3_VERIFICATION.md:3`：`状态：第一小步已实现，等待 owner 复审。`
- `EXPERIMENT_LOG.md:496`：`owner 复审 Phase 3.0 候选验证管线，确认是否进入下一小步。`
- `MASTER_PROMPT.md:40-50`：记录阶段 3.0 已实现，统一验证器/可撤销更新/留出评估未启动，
  没有“复审通过”或“批准进入下一步”的结论。

结论：**门禁条件未满足**。本报告不把 AI 自查写成 owner 批准。

## 2. 代码与文档一致性

| 检查项 | 结果 | 证据 |
|---|---|---|
| `verification.py` 是否只依赖 research/state | 是 | `selfdrive/verification.py:28` 仅导入 `research, state` |
| 是否写 `claims`/`evidence`/`relations`/`laws` | 否 | 文件内无这些写入；仅 `_append_ledger` 追加 `logs/verification.jsonl`（`verification.py:883-889,1104,1150,1247`） |
| 是否改任务/来源状态 | 否 | 冒烟前后状态 JSON 哈希一致；函数只读 `st` |
| 是否接入 `main.py` | 否 | `verify_task`/`verify_pending` 在 `selfdrive/` 内只有定义与内部调用，无主循环调用 |
| 教师裁决改名是否一致 | 是 | `skills.py:540-543` 与 `tests/test_verification_claims.py:6,29,72` 均使用 `teacher_verdict` |
| 测试数量 | 一致 | 文档写 26 项阶段 3.0 / 189 项全量；本次实跑一致 |
| 账本字段 | 基本一致 | 冒烟行包含任务/查询/来源、URL、哈希、抽取、判定、独立性、失败、版本、配置哈希、`candidate`/`none` |
| 真实网络观察 | 历史记录可读，但当前环境不可复现 | 2026-10-26 记录 HTTP 200；2026-10-27 本机 TLS 失败。应在后续文档中注明环境差异 |

未发现“文档声称已接线但代码没有”的情况。文档对“候选不是知识”“未接主循环”的边界
描述与代码一致。

## 3. 四个潜在断点

### 3.1 网页缓存仍可进入 `learn_from_text`

**代码路径**

1. `do_web_search` 把搜索结果写成 `workspace/web/web-*.md`，正文是未验证摘要，
   并明确标注 `验证状态：unverified`（`skills.py:841-851`）。
2. `scan_workspace` 同时扫描 `ARCHIVE` 与 `WEB`（`skills.py:46-58`）。
3. `do_explore_file` 可以从池中选中 web 文件；除非该文件的 surprise 全是格式噪声，
   否则调用 `learn_from_text(st, full, ...)`（`skills.py:191,208-211`）。
4. `learn_from_text` 不只做算术变换：它还会晋升概念/候选词、建立 Hebbian 关系、
   归纳规则、核对边预测（`learning.py:197-267`）。算术变换本身由探针门禁
   （`learning.py:152-194`），但普通文本概念/关系/规则没有同等门禁。
5. Teacherless 的 `BLOCKED_SKILLS` 只有 `ask_brain`、`read_brain`、`llm_consult`、
   `sibling_letter`、`explore_wider`（`teacherless.py:33-35`）；`main.py:69-74`
   也只过滤这些动作。`explore_file` 与 `web_search` 没有被禁用。

**影响**

- 任何“Teacherless 主循环从网页材料学到知识”的表述都混入了未验证网页文本，
  不能作为无教师学习证据。这是**主循环层面的阻塞性混淆**。
- 对拟议的封闭算术实验：只要使用**显式调度器**，不调用 `main.py`、不执行
  `web_search`/`explore_file`、不读取 `workspace/web`，该断点不阻塞实验本身。
  实验必须用测试断言这些路径零调用。
- `kb_remember` 在 Teacherless 下直接返回 `False`（`kb.py:15-16`），共享向量库污染
  已被阻断；问题在本地 `concepts/relations/rules` 的未验证更新，而不是共享库。

### 3.2 LLM 提案“只写入、不消费”只对了一半

**代码路径**

- `llm_bridge.save_proposals` 只把提案追加到 `state.llm_proposals`（`llm_bridge.py:266-269`）。
  Agent 代码中没有读取/验证这些提案的消费者（grep 只命中 state、teacherless 与测试），
  所以“影子库”目前是只写不读。
- 但非 Teacherless 的 `do_self_tune` 会在 `config.LLM_ENABLED` 时调用
  `llm_bridge.consult`，把 LLM 返回的 JSON 补丁直接写入 `brain/emotions.json` 或
  `brain/ask_config.json`（`skills.py:1258-1298`）。因此“LLM 只提案、从不执行”在普通模式下
  **不成立**。
- Teacherless 下 `llm_consult` 被硬禁用（`skills.py:1214`；`teacherless.py:33-35`），
  `llm_bridge.consult` 直接返回 `None`（`llm_bridge.py:228-230`），
  `llm_proposals`/`llm_calls` 必须保持 0。

**影响**

- 对使用 Teacherless 硬隔离的算术实验：不阻塞，但必须每轮断言
  `llm_proposals == 0`、`llm_calls == 0`、8 项教师/LLM 痕迹全 0。
- 对“普通模式也不依赖 LLM 执行”的表述：**阻塞**。文档不能把影子库写成已验证提案，
  也不能忽略 `do_self_tune` 的直接写入路径。

### 3.3 Semantics 自编码器与 KB 召回仍是旁路

**代码路径**

- `main.py:25` 构造 `SEM = sem.Semantics(config.BASE)`，但主循环只有
  `SEM.save()`（`main.py:195`）；没有 `SEM.learn`、`encode`、`closest`、`warmup` 调用。
- `semantics.py` 有 `train`/`learn`/`encode`/`closest` 等方法，但 Agent 代码未接线。
- `kb_recall` 没有调用者；`kb_remember` 只在 `do_explore_file` 的 `semantic_note`
  路径出现（`skills.py:109`），Teacherless 下返回 `False`（`kb.py:15-16`）。

**影响**

- 不阻塞实验，但**不能**把自编码器或 KB 召回写成有效认知能力或学习证据。
- 若使用主循环做实验，`SEM.save()` 会在 `semantics/` 写文件；实验必须隔离 `config.BASE`，
  否则会写进实时目录。拟议实验不使用 `main.py`，因此不会触发 `SEM.save()`。

### 3.4 动作失败可能被记为成功

**代码路径**

- `build_site` 在网站已生成但 `http.server` 启动失败时返回 `True`
  （`skills.py:908`）。历史 Teacherless 冒烟就观察到 `[WinError 2]` 被记为成功动作。
- 非 Teacherless 的 `do_self_tune` LLM 路径只要处理过提案就返回 `True`，
  即使 `self_mod.write_external` 全部拒绝（`skills.py:1294-1298`）。
- `main.py` 用同一个 `ok` 驱动能量、情绪与结果结算：
  `apply_energy`（`main.py:156`）、`update_emotions`（`main.py:172`）、
  `outcomes.settle`（`main.py:192`）；`outcomes.settle` 用布尔 `ok` 记成功/失败与 Brier。
- 因此假成功会抬高动作统计、能力感、情绪与能量，不能反过来当作学习证据。

**影响**

- 对拟议算术实验：只要指标使用**探针真值/留出准确率**，而不是 `ok`、动作成功率、
  情绪或轮次，就不阻塞。实验入口必须不经过 `main.py` 的奖励回路。
- 对任何“动作成功率提高=学习”的表述：**阻塞**。

## 4. 其他问题

### 4.1 现有知识更新不是只追加/可撤销

- `claims.record_transform_verdict` 直接改写 claim 的 `status` 与 `confidence`
  （`claims.py:101-112`），没有更新前/后快照，也没有回滚记录。
- `claims.add_evidence` 按 `independence_group` 去重：同一组再次出现时，无论极性，
  只增加已有证据的 `repeat_count`（`claims.py:40-83`）。算术路径的组是
  `probe:<expr>`（`claims.py:106`），所以同一表达式的后来反驳不会作为新证据写入。
  这会让“反例降级/撤销”失效。
- `conceptcard.on_transform_verdict` 会增加 L001 的支持/置信度并调用 `graduate`
  （`conceptcard.py:146-188`），但它只被教师路径调用
  （`teacher_verdict.py:57,70`），`learning._bump_transforms` 没有调用它。
  也就是说，普通无教师算术路径不会触发 L001 毕业；现有“晋升”是教师路径专属。

**结论**：这套机制**不能直接复用**到“可撤销知识更新”步骤。需要新的只追加更新账本，
记录更新前后状态、证据、验证器版本、独立证据数、适用范围、失效条件与回滚关系；
`claims.add_evidence` 的证据组极性语义也需要重新设计。

### 4.2 算术探针的封闭边界

`probes.verify_addition` 只接受：
- 操作数：中文数词 `一`..`十`（含 `两`）；
- 表达式：一个或多个操作数用 `加` 连接；
- 结果：单个中文数词 `一`..`十`。

和超过 10、结果无法用单个数词表示、或含阿拉伯数字/减法/乘法时返回 `None`
（`probes.py:7-11,29-34`）。`None` 是“不适用/未知”，**不是反驳**。预注册必须留在这个
封闭世界内，并把 `None` 路由为 `not_applicable`，不得当成 `contradict`。

### 4.3 `transform_compose` 不是通用加法器

`transform_compose` 只用已通过探针的变换做有限符号归约；它要求至少两条变换，
并且扁平化后的操作数元组必须已直接验证（`learning.py:412-458`）。它不能覆盖一般
两位数加法或任意三元组合。预注册若使用它，必须明确列出可覆盖的任务族；否则应使用
新的候选规则消除实验，而不是把 `transform_compose` 写成通用求解器。

### 4.4 Phase 3.0 没有调度器/限流

`verify_task`/`verify_pending` 是显式调用 API，没有后台循环、预算、限流或失败退避；
这与门禁第 11 条的授权范围一致。不要在本轮顺手加调度器。

### 4.5 验证账本路径依赖 `config.BASE`

`verification._append_ledger` 通过 `state.append_jsonl("verification.jsonl", row)` 写入
`config.BASE/logs`（`verification.py:883-889`；`state.py:159-162`）。实验必须在导入/运行前
把 `config.BASE` 指向隔离目录，并断言它不是仓库根；否则会写进火星实时 `logs/`。
冒烟通过复制代码到临时目录避免了这一点，正式实验也需要同样保护。

## 5. 阻塞分类

| 事项 | 是否阻塞 Phase 3.0 候选管线 | 是否阻塞封闭算术最小闭环 | 是否阻塞“可撤销知识更新” | 是否阻塞“主循环无教师学习”声明 |
|---|---|---|---|---|
| owner 无 Phase 3.0 批准记录 | — | 阻塞进入实现（本轮只做审查+预注册） | 阻塞 | 阻塞 |
| 网页缓存 → `learn_from_text` | 否 | 否（显式调度器禁用网页路径即可） | 否 | **是** |
| LLM 提案/自改直写 | 否 | 否（Teacherless 硬隔离） | 否 | **是**（普通模式表述） |
| Semantics/KB 旁路 | 否 | 否 | 否 | 否（但不得计为能力） |
| 动作失败记为成功 | 否 | 否（用探针真值做指标即可） | 否 | **是**（若用 `ok` 当证据） |
| `claims.add_evidence` 组极性去重 | 否 | 否 | **是** | 否 |
| `record_transform_verdict` 直接改写状态 | 否 | 否 | **是** | 否 |
| 探针只支持 1..10 | 否 | 设计约束（预注册已限定） | 否 | 否 |
| 无调度器/限流 | 否（符合授权） | 否 | 否 | 否 |
| 账本依赖 `config.BASE` | 否 | 实现时必须隔离 | 实现时必须隔离 | 实现时必须隔离 |

## 6. 给 owner 的拍板项

以下只是问题清单，不是裁定：

1. **Phase 3.0 是否复审通过？** 若通过，请明确写入 `MASTER_PROMPT.md` 或
   `EXPERIMENT_LOG.md` 的批准记录；本轮不代写。
2. **是否接受“先预注册、再实现”的顺序？** 预注册草案见 `PREREG_ARITHMETIC.md`。
   在 owner 确认前，不实现候选规则消除、不写自动知识更新、不接主循环。
3. **封闭算术实验是否采用“预注册候选规则集内的证据消除”作为第一个最小闭环？**
   该设计的优点是确定性、可审计、可回滚；局限是正确规则 `sum_all` 是代码级先验，
   因此只能证明“规则选择/更新管线在固定任务集上有效”，不能证明系统从零发明加法。
   如果 owner 要求更强的“从数据构造规则”，需要另写预注册并扩大候选空间/任务难度。
4. **是否要求在进入步骤 2 前先修 `claims.add_evidence` 的极性去重？**
   本报告认为它阻塞“可撤销知识更新”，但不阻塞 Phase 3.0 候选管线。若 owner 希望
   先修，需要单独授权一个 scoped fix。
5. **是否要求在主循环实验中加网页路径隔离？**
   若未来要让 Teacherless 主循环参与学习评估，必须先阻止 `explore_file` 消费
   `workspace/web`，或把网页材料严格限制为“只读候选、不进 `learn_from_text`”。

## 7. 证据等级与限制

- **代码证据**：189 项测试、26 项阶段 3.0 测试、compileall、静态调用链核查。
- **隔离证据**：22/22 冒烟检查、状态不变、无 claims/evidence/relations/laws、
  任务仍 `pending_verification`、来源仍 `unverified`。
- **没有**真实学习实验证据；**没有**留出评估；**没有**学习增益结论。
- 本报告是 AI 复审，不是 owner 批准；所有“通过”表述仅指受审代码在本次测试范围内
  通过，不代表阶段验收或学习能力成立。
