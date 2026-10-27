# Phase 3.0：网页候选验证管线（最小闭环）

状态：**第一小步已实现；2026-10-27 完成 AI 复审与基线复验，未发现 owner 复审通过记录，
等待 owner 复审。** 复审报告见 `PHASE3_REVIEW.md`；下一步预注册草案见
`PREREG_ARITHMETIC.md`（DRAFT，未获批准）。本阶段只证明“候选材料可以被安全抓取、
抽取、判定并写入可追溯的只追加账本”，**不证明系统学会了知识，也不证明发生进化**。

授权范围见 `MASTER_PROMPT.md` 门禁第 11 条：不重写研究任务生成、查询调度或主循环；
不自动晋升 `claims`/`relations`/`laws`；在可撤销知识更新与测试就绪前，结论只能是候选。

## 1. 这一阶段解决什么

阶段 2 的终点是：

```
研究任务 → 定向查询 → 搜索结果 → 来源账本（unverified）
```

阶段 3.0 补上下一段，但仍不闭合知识更新：

```
pending_verification 任务
        |
        v
按任务取出来源（当前索引 + 任务快照，重新绑定 query_id）
        |
        v
安全抓取（公网校验/重定向复检/超时/大小上限）
        |
        v
正文抽取（HTML/纯文本，去导航与脚本噪声）
        |
        v
确定性线索判定（支持/反驳/不确定）+ 独立性分组
        |
        v
logs/verification.jsonl（只追加，conclusion_status=candidate，promotion=none）
```

**明确不做**：不写 `claims.add_evidence`，不更新关系边或概念置信度，不晋升定律，
不改变任务状态/来源 `verification` 字段，不触发主循环，不做留出评估，不调用教师、
LLM、兄弟信箱、共享向量库或交互输入。

## 2. 代码落点与命名

| 文件 | 变更 |
|---|---|
| `selfdrive/verification.py` | 新增：安全抓取、正文抽取、线索判定、独立性分组、只追加账本、汇总与待验证任务扫描 |
| `selfdrive/teacher_verdict.py` | 由原 `verification.py` 改名：只保留教师 `[VERDICT]` 与确定性探针的旧路径 |
| `selfdrive/skills.py` | `do_read_brain` 改为导入 `teacher_verdict`，行为不变 |
| `tests/test_verification_claims.py` | 跟随改名，改用 `teacher_verdict.apply_verdict` |
| `tests/test_verification_phase3.py` | 新增 26 项 Phase 3.0 测试 |
| `selfdrive/__init__.py` | 版本 `9.25.0`，模块地图更新 |
| `selfdrive/teacherless.py` | 仅更新 docstring/manifest 说明，声明阶段 3.0 只产出候选 |

改名原因：旧 `verification.py` 处理的是教师意见与环境探针，新模块处理网页候选证据。
两者语义不同，混在同一模块会让“教师裁决”和“网页候选”难以审计。改名不改变旧路径行为，
全部既有测试仍然通过。

## 3. 入口与返回值

```python
verification.verify_task(st, task, max_sources=3, fetcher=None, ledger=None)
verification.verify_pending(st, limit_tasks=3, max_sources_per_task=3,
                            fetcher=None, ledger=None)
verification.summarize_task(rows, task_id)
verification.latest_candidates(st, ledger=None, limit=20)
```

- `verify_task()`：验证单个任务的前 `max_sources` 个来源，返回
  `task_id`/`sources`/`appended`/`duplicates`/`failures`/`records`。
- `verify_pending()`：按“当前任务优先、优先级高、更新轮次新”的顺序扫描
  `verification=pending_verification` 的任务，也包含 `research.archived_pending`
  中仍有材料的归档任务；返回逐任务结果、追加条数与最新候选汇总。
- `sources_for_task()`：从当前来源索引与任务自带快照还原来源，只读；同一 URL 的
  `source_id` 被多个任务复用时，按本任务的查询记录重新绑定 `query_id`，避免把 B 任务
  的来源记到 A 任务的查询上。
- `summarize_task()`/`latest_candidates()`：只读账本，按任务汇总候选结论。

这两个入口目前**没有接入 `main.py`**，是可调用 API；不存在自动后台抓取。

## 4. 只追加账本

路径：`logs/verification.jsonl`；格式：`alive-verification/1`。账本只追加，不重写、
不删除。每行至少包含：

| 类别 | 字段 |
|---|---|
| 关联 | `task_id`、`task_kind`、`task_question`、`query_id`、`source_id` |
| 地址 | `url`、`final_url`、`redirects`、`redirect_rejection`、`independence_group` |
| 抓取 | `http_status`、`content_type`、`charset`、`content_encoding`、`body_bytes`、`body_sha256`、`fetched_at`、`truncated` |
| 抽取 | `text_sha256`、`extraction_status`、`extraction_detail`、`title` |
| 判断 | `evidence_excerpt`、`excerpt_kind`、`judgment`、`judgment_reason`、`basis`、`matched_terms`、`support_cues`、`contradict_cues` |
| 独立性 | `independent`、`evidence_counted`、`duplicate_of`、`independent_reason` |
| 失败 | `failure_kind`、`failure_key` |
| 版本 | `verifier_version`、`config_sha256`、`format`、`verification_id`、`event_at` |
| 边界 | `conclusion_status="candidate"`、`promotion="none"` |

`body_sha256` 由实际收到的正文重新计算，不信任抓取器自报的哈希；`text_sha256` 覆盖
抽取后的正文文本。`title` 与 `evidence_excerpt` 只是证据摘录，不是完整原文存档；
完整正文不落盘，避免把不可信网页内容当成长期知识。

### 幂等与失败去重

- 成功记录键：`(task_id, source_id, body_sha256, verifier_version)`。
  重复执行同一任务+来源+正文+验证器版本，返回 `duplicate`，不追加、不增加计数。
- 抓取失败键：`(task_id, source_id, url, failure_kind, verifier_version)`。
  同一失败重复执行返回 `duplicate_failure`。
- `verification_id` 由成功键或失败键派生；失败键中包含账本当前行数，仅用于 ID 唯一，
  不影响按上述键去重。
- 验证器内部异常会被 `_verify_task_with_index` 捕获，写入
  `failure_kind=verifier_exception` 的显式记录；其他来源的记录不受影响。

## 5. 安全抓取

`fetch_public_url(url, resolver=None, http_get=None)` 的约束：

1. **URL 语法**：只接受绝对 `http`/`https`；拒绝空值、控制字符、空白、带用户名/密码、
   非法 IDNA/主机标签、全数字伪 IP、`localhost`/`.local`/`.internal`/`.lan`/`.home`
   等本地后缀，以及 80/443 之外的端口。
2. **DNS 公网校验**：解析结果必须全部是 `ipaddress.is_global` 地址；混合结果只要有
   非公网地址就整体拒绝。字面 IP 同样校验。`resolver` 可注入用于测试。
3. **固定 IP 连接**：连接使用已校验的 IP，同时保留原主机名做 Host 头与 TLS SNI/证书
   校验；`http_get` 可注入用于测试。
4. **重定向**：最多 5 跳；每一跳重新做 URL 与 DNS 公网校验；拒绝非 HTTP(S)、非
   80/443、登录页/不可解析目标；检测重定向循环；跨域跳转的最终域名决定独立性分组。
5. **超时与大小**：单跳连接/读取超时 10 秒，整体截止 30 秒（每跳取剩余时间）；
   正文最多 2,000,000 字节，超出标记 `truncated`；只接受
   `text/html`、`application/xhtml+xml`、`text/plain` 与 `identity` 编码。
6. **显式失败**：`invalid_url`、`non_public_address`、`dns_error`、`timeout`、
   `tls_error`、`protocol_error`、`network_error`、`unsafe_redirect`、
   `redirect_loop`、`too_many_redirects`、`http_error`、`fetcher_exception` 都写账本，
   不静默跳过。

网页内容始终按不可信输入处理：不执行脚本，不解析网页中的指令，不修改配置/权限，
不把正文当代码或命令。

## 6. 正文抽取

`extract_document(fetch_result)` 的状态：

| 状态 | 含义 |
|---|---|
| `ok` | 类型/编码受支持，正文至少 20 字符 |
| `empty` | 空正文或有效文本不足 20 字符 |
| `unsupported_content_type` | 非 HTML/XHTML/纯文本 |
| `unsupported_content_encoding` | 非 `identity` 编码 |
| `extraction_error` | HTML 解析器异常 |
| `fetch_failed` | 上游抓取失败 |

HTML 解析会跳过 `script`/`style`/`noscript`/`nav`/`footer`/`aside`/表单等噪声节点；
正文折叠空白后最多保留 200,000 字符。抽取失败仍写一条 `judgment=uncertain` 的明确记录，
不猜测内容。

## 7. 候选判定

规则版本：`terms-cues-v1`。步骤：

1. 从任务 `target` 提取目标词（概念、表达式拆分项、主题），最多 8 个。
2. 目标词按大小写不敏感匹配；英文按词边界匹配。至少命中 `min(2, 词数)` 个目标词。
3. 至少两个目标词在同一句共现；方向线索须在目标词 60 字符窗口内。
4. 线索分强支持、弱支持、反驳三组（中英文）。同一位置重叠时取最长线索。
5. 判定：
   - 强支持 + 反驳同时出现 → `uncertain`（`conflicting_cues`）
   - 只有反驳 → `contradict`
   - 只有强支持 → `support`
   - 只有弱支持 → `uncertain`（`weak_support_cue`）
   - 无方向线索/词不共现/目标词不足 → `uncertain`，并给出原因

`basis` 记录规则版本、原因、命中词、线索与方向句数量。判定是**候选级文本线索**，
不理解反讽、条件句、否定范围或来源权威性；因此 `support` 不是“命题为真”。

## 8. 来源独立性

独立性分组按**最终落点 URL**计算，而不是原始搜索 URL：

- 可注册域：使用保守的多级后缀表；`github.io` 等托管平台整站合并；
- IP：按 `ip:<地址>` 分组；
- 无法解析/非法 URL：返回空组，**不计独立证据**；
- `body_sha256` 或 `text_sha256` 相同的转写/镜像，在同一任务内只算一次
  （`duplicate_of` 指向首条记录）。

只有“抽取成功 + 判定为 support/contradict + 首次出现的来源组 + 非重复内容”才把
`independent=true`、`evidence_counted=true`。同域、同正文、空组、抽取失败或仅
`uncertain` 的记录都不计入独立证据。策略是宁可少算，不把同源当独立。

## 9. 候选汇总与“不晋升”保证

`summarize_task()` 只汇总当前 `verifier_version` 的行：

- 支持组数/反驳组数只统计 `evidence_counted=true`；
- `uncertain_sources` 只统计抽取成功的 uncertain 行；
- 失败抓取、抽取失败、重复内容分别计数；
- 结论为 `candidate_support` / `candidate_contradict` / `candidate_conflict` /
  `candidate_uncertain`，并带 `conclusion_status="candidate"`、`promotion="none"`。

阶段 3.0 不提供知识晋升门禁，因此没有“自动晋升”路径：代码中没有
`claims.add_evidence`、关系/定律/概念置信度写入，也不修改任务或来源状态。
测试会在每次端到端执行后比较状态 JSON，确认 `claims`/`evidence`/`relations`/`laws`
仍为空，任务仍为 `pending_verification`，来源仍为 `unverified`。

## 10. Teacherless 边界

`verification.py` 只导入 `research` 与 `state`，不导入 `teacherless`、`llm_bridge`、
`kb` 或任何教师通道模块。抓取器没有交互输入入口；网页内容不会进入教师/LLM 证据。
隔离冒烟在全新 Teacherless 目录中运行，8 项教师/LLM 痕迹计数保持为 0。

注意：阶段 3.0 的候选账本不会写共享向量库；普通模式下搜索材料的 `kb_remember()`
副作用仍按阶段 2 的说明处理，任何共享库召回都不能自动视为已验证证据。

## 11. 自动化测试

`tests/test_verification_phase3.py` 共 **26 项**，使用临时目录与注入式
`fetcher`/`resolver`/`http_get`，不需要外网：

- 端到端：真实 `research.ensure_tasks` → `next_query` → `record_execution` →
  来源账本 → `verification.verify_task`，检查任务/查询/来源/URL/正文哈希/获取时间/
  抽取/判定/依据/配置哈希/验证器版本，并断言任务仍待验证、来源仍 `unverified`、
  状态 JSON 不变。
- 幂等：重复成功不追加、不增加证据计数；重复抓取失败按失败键去重。
- 非法 URL：空 URL 不调用抓取器；`ftp://` 与空 URL 都写显式失败；私有/畸形 URL
  与 `localhost` 在 DNS 前拒绝。
- 抓取安全：重定向目标复检、公开重定向被跟随并记录、网络失败/超时/抓取器异常、
  正文大小截断、读取截止时间。
- 抽取失败：不支持的类型、空 HTML、不支持的编码都写 `uncertain` 记录。
- 独立性：同注册域只算一次；跨域同正文只算一次；重定向落点域名决定分组；
  无法解析的主机不计独立。
- 支持/反驳/冲突：两条独立来源分别支持与反驳 → `candidate_conflict`；
  只有共现没有方向线索 → `uncertain`。
- 共享来源：同一来源 ID 被两个任务复用，验证 B 时账本绑定 B 的 `query_id`。
- 归档任务：`verify_pending` 同时消费当前与归档的待验证任务。
- 异常安全：中途验证器异常不破坏前一条成功记录，异常本身写账本。
- 配置自描述：配置快照包含版本与全部参数，哈希稳定。

全量测试：

```bash
py -m compileall -q selfdrive tests agent_v9.py semantics.py    # 通过
py -m unittest discover -s tests                                # 189 项通过
```

其中阶段 3.0 新增 26 项，阶段 2/2.1 为 64 项，阶段 1.1 为 99 项。上述
`compileall` 与全量测试也在只复制 `.py` 的纯代码临时副本中重跑（189 项通过），
说明测试不依赖仓库根目录的火星实时 `state.json`/`brain`/`logs`/`workspace`。

## 12. 隔离冒烟与证据报告

本地临时目录（`runs/` 已忽略，不提交，不接触实时火星状态）：

- 验证管线冒烟：`runs/phase3_verification_20261026/verify/`
  - 代码指纹 `a311f004e44b50aa5c5258892693c044ef86312aa9c1780905fc941984573ad4`；
  - 验证器配置哈希 `af82789a82e6701b60e271cc61b01475208a67276d458b23b697812655ad6ae2`；
  - 合成预测落空任务 → 两条来源（支持域/反驳域）→ 真实 `research.record_execution`
    入账 → `verify_pending` 假 HTML 抓取；
  - 22 项检查全为 true：状态不变、任务仍待验证、来源仍 unverified、
    结论 `candidate_conflict`（支持 1/反驳 1）、无 claims/evidence/relations/laws、
    无教师/LLM 痕迹、重复执行幂等、非法 URL 显式失败且不晋升；
  - 另做一次真实网络观察 `https://example.com/`：HTTP 200、正文 577 字节、
    标题 `Example Domain`、文本 156 字符。该观察**不参与**冒烟通过/失败判定。
- Teacherless 冒烟：`runs/phase3_verification_20261026/teacherless/`
  - `from_zero` 新实验 `TL-PHASE3-20261026`，代码版本 `9.25.0`，指纹同上；
  - 3 轮；`status.clean=true`、`code_drift=false`、`seed_problems=[]`、
    `violations={}`；
  - 8 项教师/LLM 痕迹全 0，`blocked_channel_log_counts={}`；
  - 第 1/3 轮辅助动作 `build_site` 在本机启动服务失败（`[WinError 2]`，
    缺少启动器），属环境限制，不影响隔离边界；
  - 该冒烟只证明验证模块没有破坏 Teacherless 隔离，不证明学习发生。
- 测试报告与证据汇总：`runs/phase3_verification_20261026/test_report.json`、
  `report.json`（代码指纹、配置快照、测试、两段冒烟）。
- 2026-10-27 复验：在全新临时副本 `runs/phase3_review_20261027/verify/` 重跑，
  22/22 项检查 true，代码指纹与配置哈希同上；可选真实网络观察
  `https://example.com/` 在本机因本地 CA 缺失返回 `tls_error`（失败关闭，不参与
  通过判定）。该复验只重复隔离证据，不改变阶段结论。

## 13. 已知风险与限制

1. **判定不是理解**：线索规则可能被反讽、条件句、否定范围或模板化页面误导；
   `support`/`contradict` 都是候选标签。
2. **没有来源权威性模型**：不评估作者、机构、发布时间、引用链或同行评审；
   论坛、营销页与原始资料在账本中同等对待，只靠独立性分组避免重复计数。
3. **独立性是保守近似**：同域不同文章只算一个来源；托管平台整站合并；跨域转载
   若正文不同仍可能被当作两个来源；正文哈希只在同一任务内去重。
4. **覆盖范围有限**：不执行 JavaScript，不解析 PDF/Office；正文/文本上限会截断；
   只接受 `identity` 编码。动态站点可能得到空壳或明确失败。
5. **每个任务每次只取前 3 个来源**：重复调用是幂等的，但不会自动推进到第 4 个来源；
   来源预算/重试策略尚未实现。
6. **没有接入主循环**：需要显式调用 `verify_task`/`verify_pending`；没有调度、
   限流或自动重试。
7. **没有可撤销知识更新**：因此本阶段不晋升；统一验证器注册表、命题/关系状态迁移、
   回滚与留出评估都未实现。
8. **证据摘录不是原文存档**：账本保存摘录与哈希，不保存完整网页；后续复核需要
   重新抓取，内容可能已变化。

## 14. 下一门槛

1. owner 复审 Phase 3.0 候选验证管线与 `PHASE3_REVIEW.md`，明确是否批准；没有批准
   记录前不进入实现。
2. owner 确认 `PREREG_ARITHMETIC.md` 草案；未批准前不实现候选规则消除、不写自动
   知识更新、不接主循环。
3. 设计统一验证器注册表：网络候选、算术探针、程序测试、形式规则各自声明适用范围，
   网络证据不得替代可执行验证。
4. 设计可撤销知识更新：命题/关系边更新必须带来源、验证器版本、独立证据数、
   回滚记录与失效条件，并有回归测试。
5. 决定是否/如何把候选验证接入主循环或显式实验调度（预算、限流、失败退避）。
6. 建立留出任务与对照评估，比较“有研究队列 + 候选验证”相对基线的增量。
7. 在上述完成前，`candidate_support` 只是候选；任何“学会/进化”表述都不成立。
