# Phase 2：研究任务队列与定向搜索规划

状态：**阶段 2 第一小步已实现，并完成三轮审查修正、历史保留补丁与全链路审计补丁
（基线提交 `2500c4e`）。** 阶段 3.0 已在此基础上实现候选验证管线
（`selfdrive/verification.py`，只产出候选、不晋升知识），见
`PHASE3_VERIFICATION.md`；统一验证器注册表、可撤销知识更新、留出评估与策略优化
仍未启动。

## 1. 这一阶段解决什么

旧版搜索的输入是“最近出现的词”，输出是一篇网页摘录；它无法回答：

- 为什么现在要搜这个词？
- 这次搜索和哪个知识缺口有关？
- 上次搜过没有？为什么又搜一遍？
- 搜到的东西算不算已经验证？

阶段 2 只补这条链路的前半段：

```
本地知识缺口 → 研究任务（可解释优先级） → 目的化查询 → 去重/失败改写
            → 现有 web_search 技能 → 候选材料与来源账本（unverified）
```

**明确不包含**：正文深度提取、证据门禁、命题状态更新、定律晋升、统一验证器、
留出任务评估。搜索成功只代表“拿到了候选材料”，不改变任何 `claims`/`laws`/概念置信度。

## 2. 代码落点

| 文件 | 变更 |
|---|---|
| `selfdrive/research.py` | 新增：任务队列、查询规划、去重、失败改写、来源账本、配置快照 |
| `selfdrive/config.py` | 新增 `RESEARCH_*` 上限与版本号 |
| `selfdrive/state.py` | `STATE_VERSION=4`，新增 `research` 默认段 |
| `selfdrive/main.py` | 每轮 `research.ensure_tasks(st)`（异常不阻塞主循环） |
| `selfdrive/skills.py` | `do_web_search` 消费研究查询；SERP 解析、主机回退、Bing 跳转链还原 |
| `selfdrive/decision.py` | 有开放研究任务时轻微抬升 `web_search` 权重；搜索禁用/冷却仍优先 |
| `selfdrive/working_memory.py` | 工作记忆暴露只读 `research_focus`（最多 3 条） |
| `selfdrive/__init__.py` | 版本 `9.25.0`（阶段 3.0 同步更新） |

阶段 3.0 新增 `selfdrive/verification.py` 消费本阶段的 `pending_verification`
任务与来源账本；原教师 `[VERDICT]`/探针路径改名为 `selfdrive/teacher_verdict.py`。
阶段 2 本身不写验证账本，也不改变 `materials_found` 的“未验证”语义。

## 3. 任务结构

每个任务至少有：

```json
{
  "id": "RT:<stable-hash>",
  "kind": "prediction_miss | claim_evidence_gap | relation_unverified | isolated_concept | exploration",
  "kind_label": "预测落空",
  "question": "《A》和《B》之间缺少什么中间概念或条件？……",
  "reason": "预测落空：第 N 轮下注……结算时从未同现。",
  "target": {"type": "relation|claim|concept|exploration", "...": "..."},
  "priority": 0.80,
  "status": "open | active | materials_found | exhausted | blocked | abandoned",
  "verification": "unverified | pending_verification",
  "attempts": [{"query_id": "...", "purpose": "...", "outcome": "..."}],
  "attempt_history": [{"kind": "relation_unverified", "attempts": ["..."]}],
  "kind_history": [{"from": "relation_unverified", "to": "prediction_miss"}],
  "query_ids": ["RQ:..."],
  "source_ids": ["RS:..."],
  "result_count": 0
}
```

- 关系任务按**关系键**稳定哈希（`relation|A|B`），同一关系只保留一个任务；
  其他任务按 `kind + 目标` 稳定哈希，同一缺口不会每轮新建任务。
- 同一关系先有 `relation_unverified`、后出现 `prediction_miss` 时，任务**原地升级**：
  `kind`/问题/原因/目标切换为更强信号，旧 `attempts` 归档进 `attempt_history`，
  `rewrites` 重置，未执行查询标记 `cancelled`；`query_ids`/`source_ids`/`result_count`
  保留。升级写 `task_upgraded` 与 `kind_history`，不会静默丢掉研究历史。
  已 `exhausted` 或 `materials_found` 的同类任务也会被更强信号重新打开；
  同强度/更弱信号不会降级，也不会重开已闭合任务。
- `status=materials_found` 只表示“有候选材料待验证”，不是“已学会”。
  `verification` 只会变成 `pending_verification`。
- 上限：开放/活跃任务 24，总任务 80，每轮最多新建 3 个；所有创建入口
  （含探索回退）共用 `_create_task` 的同一组硬上限，拒绝时写
  `task_creation_blocked`。超出按优先级与时间修剪。
- **候选不再按种类限额**：`prediction_miss`/`claim_evidence_gap`/
  `relation_unverified`/`isolated_concept` 全部参与候选扫描，由优先级与
  `RESEARCH_NEW_PER_ROUND=3` 的新建预算控制；既有任务无论本轮新建额度是否
  耗尽，都继续执行 `_refresh_task()` / 原地升级，不会永久占死“新候选”配额。
- **预测落空补录**：任务扫描覆盖 `edge_preds[-300:]`，落空信号另存
  `miss_backlog`（上限 120），按概念对去重；即使原信号已滑出候选使用的
  `edge_preds[-60:]` 窗口，仍可补建 `prediction_miss` 任务。已有该任务时
  自动消费对应 backlog 项。超出上限写 `miss_backlog_evicted`。
- **旧状态迁移（schema 3）**：`state.research.schema_version` 低于 3 时，
  `ensure_state()` 执行显式迁移。v1→v2 继续完成阶段 2.0 关系任务 ID 归一、
  查询/来源 `task_id` 同步、`content_sha256`→`title_excerpt_sha256`；v2→v3
  处理升级语义与旧计划查询。所有修复只依据可审计证据：`kind_history` 的
  迁移轮次、查询的 `created_round`/状态、任务当前 `attempts`。缺失迁移记录
  或轮次非法时不做猜测，写 `task_migration_unrecoverable` 并说明原因。
  迁移可重复执行，第二次不再产生变更。
- **合并语义与正常升级一致**：先确定最终种类（最强信号），再选择生命周期
  载体。载体种类发生变化时，其当前尝试归档、`attempts`/`rewrites` 重置；
  `planned` 且种类不同的来源查询全部标记 `cancelled` + `failure=superseded`，
  查询 ID 写入 `kind_history`；同种类查询、已执行查询、`query_ids`/
  `source_ids`、候选材料与 `pending_verification` 均保留。生命周期仍按
  “有活跃任务取活跃；否则有材料取 `materials_found`；仅全部闭合才保留终止态”
  合并，合并后的可执行任务仍在 `_viable_tasks()` 中。
- **历史保留**：`attempt_history` 与 `kind_history` 都按 `round` 排序，只保留
  最近 `HISTORY_KEEP=5` 条。`task_id_migrated` 分别记录双方历史条数、归档的
  当前尝试条数与归档行数、最终保留/丢弃条数；`attempt_history_truncated` /
  `kind_history_truncated` 记录截断数，保证
  “原有 + 导入 + 归档 = 保留 + 丢弃”可对账。
- **待验证材料不静默删除**：任务超出总上限时，有材料的任务写入
  `archived_tasks`（上限 `RESEARCH_ARCHIVE_KEEP=40`），快照包含目标、种类、
  查询/来源快照、验证状态、尝试与历史、归档轮次与原因；`archived_pending()`
  与 `focus()` 可继续发现这些任务。归档超出上限写 `task_archive_evicted`，
  不含材料的任务才直接丢弃。
- 迁移写 `task_id_migrated` / `source_fields_migrated` /
  `research_schema_migrated`；历史 JSONL 不重写。阶段 2.0 的实验状态可以继续
  使用，不需要伪装成“从零”状态；若要做全新基线，仍应新建 `from_zero`
  目录，二者不得混用。

## 4. 任务来源

| 来源 | 触发条件 | 为什么值得研究 |
|---|---|---|
| `prediction_miss` | `edge_preds` 中 `status=miss` | 下注落空说明缺中间概念、条件或反例 |
| `claim_evidence_gap` | 命题独立支持与独立反证都为 0 | 命题没有任何独立证据 |
| `relation_unverified` | 关系边 `n<=1` 且 `w<=2` | 边只由同现建立，缺独立验证 |
| `isolated_concept` | 概念已晋升（`n>=3`）但度数为 0 | 概念无法参与推理 |
| `exploration` | 队列无可用任务且有已晋升概念作回退主题 | 有界探索，不作为常规主路径 |

优先级在 `KIND_BASE` 基础上，对最近活跃概念（`recent_active`）和 `last_kw`
小幅加分；所有任务生成都写 `logs/research.jsonl` 的 `task_created`，包含
question/reason/priority/target。

## 5. 定向查询规划

同一任务按类型生成最多 3 条**目的不同**的查询：

| 任务类型 | 查询目的（依次） |
|---|---|
| `prediction_miss` | `bridge` 找中间概念 → `mechanism` 查机制 → `relation` 查关系 |
| `claim_evidence_gap` | `evidence` 找支持/反驳 → `counterexample` 找反例 → `definition` 查定义边界 |
| `relation_unverified` | `relation` → `evidence` → `counterexample` |
| `isolated_concept` / `exploration` | `definition` → `context` → `mechanism` |

查询用自然中文连接（如 `A和B有什么关系`），不是把最近的词直接丢给搜索引擎。
`exploration` 的回退主题必须已晋升为概念；文件名、日记尾词等“最近出现过”的词
不构成知识缺口，`do_web_search` 会直接返回无查询而不是又回到旧行为。
每条查询写入 `queries` 与日志，带 `task_id`、`purpose`、`selection_reason`，
因此可以回答“这次为什么搜这句”。

## 6. 重复控制与失败改写

- 规范化：小写、去空白；完全相同的查询直接判重。
- 相似判重：字符二元组 Jaccard（阈值 `RESEARCH_DUP_JACCARD=0.8`）。
- 去重窗口：最近 40 条已执行查询；被拦的查询写 `query_duplicate_blocked`，
  计数进入 `stats.duplicates_blocked`。
- 失败分类：
  - `network_error`：两个 Bing 主机都不可达 → 记失败，下一次换目的，不改写。
  - `serp_blocked`：返回首页/反爬页/解析不到结果页 → 记失败，下一次换目的，不改写。
  - `no_results`：搜索引擎明确返回“没有与此相关的结果” → 允许 **1 次**
    `broadened` 改写（`综述/案例/教程`），改写理由与父查询都入账。
- 单任务最多 3 次尝试；用尽后 `materials_found`（有候选）或 `exhausted`（无候选）。
- 所有失败都不会被当成“知识不存在”，只记录“这次查询没拿到可用材料”。

## 7. 搜索接口与来源账本

`do_web_search(st)` 现在是研究任务的消费者：

1. `research.next_query(st, fallback_topic=...)` 取查询；
2. 依次尝试 `cn.bing.com` 与 `www.bing.com`；单台主机明确返回“无结果”不会
   终止另一台，只有两台都没有结果才按失败结算；
3. 解析 `b_algo` 结果；Bing 的 `/ck/a?...u=a1<base64>` 跳转链还原为真实 URL；
   解析阶段即丢弃空 href、相对路径、非 HTTP(S) URL，并去重；
4. 成功时写 `workspace/web/web-*.md`（含任务 ID、查询 ID/目的、`unverified` 标记）；
5. 通过 `research.record_execution` 入账；入账时再次校验 URL，只把实际写入
   来源账本的 ID 计入成功。没有任何有效来源时不写 `materials_found`：
   空结果记 `no_results`，只有无效链接记 `invalid_results`。

来源记录同时写两处：

- `state.research.sources`（索引，最多 200 条）：id、task_id、query_id、purpose、
  url、title、title_excerpt_sha256、fetched_at、serp_url、serp_sha256、source_type、
  verification、config_sha256、fetch_count；
- `logs/research_sources.jsonl`（逐次追加，`format=alive-research-source/2`）：
  同一 URL 每次被搜到都会留一条带 task/query 的完整记录；同一 URL + 摘要哈希
  在索引中只占一条并累加 `fetch_count`。

哈希覆盖范围必须按字段名理解：

| 字段 | 输入 | 不是什么 |
|---|---|---|
| `serp_sha256` | Bing 搜索结果页 HTML 全文 | 不是目标网页正文 |
| `title_excerpt_sha256` | 结果条目的 `title + "\n" + excerpt`（excerpt 最多 400 字符） | **不是网页正文哈希** |

阶段 2 不提取正文，因此没有 `page_content_sha256`；`title_excerpt_sha256` 不能当成
网页内容指纹。阶段 3.0 的 `verification.py` 在抓取后另算 `body_sha256`（实际正文
字节）与 `text_sha256`（抽取文本），两者只写入验证账本，不写回阶段 2 的来源索引。

`source_type=web_runtime`、`verification=unverified` 是硬编码值。阶段 2 不写
`claims.add_evidence`，不更新 `laws`，不因“多个网页说法相似”晋升任何知识。

**来源成功不变式**：`record_execution()` 的成功、`result_count`、`source_ids` 与
`pending_verification` 只由“实际写入账本的有效来源 ID”决定，不使用
`bool(ok and results)` 或原始结果条数。URL 为空、相对路径、协议不是 HTTP(S)、
解析失败或重复的条目被拒绝并计入 `stats.source_candidates_rejected`；即使
SERP 有若干条目但全部无效，查询也按 `invalid_results` 失败结算，任务不会被标成
`materials_found`。

## 8. 配置指纹

代码指纹只能覆盖 `.py` 文件，覆盖不到运行期配置。阶段 2 额外记录影响研究调度与
搜索策略的参数快照：

- 代码常量：任务/查询/尝试/改写/去重/历史/落空补录/归档上限，
  `RESEARCH_CONFIG_VERSION=research-config-v3`；
- `brain/ask_config.json` 的 `web_search`（enabled/top_k/cooldown_rounds）；
- `ask_config.json` 的 `research` 段（若存在）。

`research.record_config` 只在规范化 JSON 的 SHA-256 变化时追加
`state.research.config_history`（最多 20 条），并写 `config_recorded` 日志；
任务、查询和来源记录创建时都写入当时的 `config_sha256`。这解决了
`do_self_tune()` 修改 `ask_config.json` 后代码指纹不变、实验条件已变的问题
（在阶段 2 范围内）。

## 9. Teacherless 边界

- `research.py` 不导入 `teacherless`、`llm_bridge`、`kb` 等通道模块；
- 任务只从本地 `state` 的预测/命题/关系/概念生成；
- 搜索在 Teacherless 中按既有策略允许（`observation_allowed`），但结果仍是
  `unverified` 候选材料；
- 冒烟测试断言研究链路运行前后 8 项教师/LLM 痕迹计数不变。
- **共享记忆副作用**：`do_web_search()` 成功后仍会调用 `semantic_note()` →
  `kb_remember()`。Teacherless 下 `kb_remember()` 直接返回 `False`，候选材料
  不会写入共享向量库；普通模式下仍可能写入本地共享记忆库。阶段 3 不得把共享库
  召回结果自动视为已验证证据。

## 10. 测试与实测

### 自动化测试

```bash
py -m compileall -q selfdrive agent_v9.py tests
py -m unittest discover -s tests
```

- 全量：**189 项通过**（阶段 1.1 为 99 项，阶段 2/2.1 新增 64 项，阶段 3.0 新增
  26 项）。阶段 2 自身的 64 项见 `tests/test_research.py`；阶段 3.0 的
  26 项见 `tests/test_verification_phase3.py` 与 `PHASE3_VERIFICATION.md`。
- `tests/test_research.py`：64 项，覆盖 owner 指定的固定案例：
  - 同一研究目标生成 3 条目的不同的具体查询；
  - 重复主题/相似查询被抑制并计数；
  - `no_results` 生成一次更宽改写；`network_error`/`serp_blocked` 换目的不改写；
  - 每次查询都能回溯到 `task_id`，每次来源都能回溯到 `query_id`；
  - 关系任务先建立、后出现预测落空时原地升级，尝试历史不丢失；
  - 已 `exhausted` / `materials_found` 的关系任务被更强信号重新打开；
  - 探索回退受总任务、开放任务与每轮新增三组硬上限约束；
  - 每轮新建额度用尽后，排在后面的既有弱关系任务仍被预测落空信号升级；
  - 阶段 2.0 的关系任务 ID、查询/来源 `task_id` 与旧来源字段可迁移，
    新旧任务碰撞时合并且不丢历史关联；
  - 终止态旧任务与活跃旧任务碰撞时，合并后仍保持可执行并出现在
    `_viable_tasks()` 中，不会继承旧 `exhausted`；
  - 终止态旧任务与 `materials_found` 旧任务碰撞时，合并后保留
    `materials_found` + `pending_verification`，候选材料仍能被发现；
  - 被合并任务已有的 `attempt_history` 迁入主任务，当前尝试、查询/来源与
    生命周期状态同时保留；
  - `attempt_history`/`kind_history` 按轮次有界保留最近 5 条（包括单个旧任务
    自带的超长 `kind_history`），截断写日志，迁入/归档/保留/丢弃数量可对账；
  - 四类候选在多轮中都能建任务，不被每轮新建预算永久饿死；预算耗尽后既有
    任务仍可刷新/升级；`edge_preds` 窗口淘汰的落空由 `miss_backlog` 补建，
    backlog 超限按上限淘汰；
  - 真正的 v2 状态（归一化 ID、`schema_version=2`、旧种类 `planned` 查询）
    迁移到 v3：按 `kind_history` 轮次归档旧尝试、取消旧计划查询、可重新规划
    同文本新查询，重复迁移幂等；缺失/非法迁移记录写
    `task_migration_unrecoverable`；
  - 载体换种类时归档并重置尝试、取消不同种类的计划查询，保留已执行查询、
    来源与生命周期；同种类计划查询不取消；多任务合并时最新历史保留且
    双方计数对账；
  - 来源解析与入账双重 URL 校验：缺失/相对/非 HTTP(S)/重复链接不成功，
    仅有无效结果记为 `invalid_results`，不进入 `materials_found`；
  - `cn.bing.com` 明确无结果时继续尝试 `www.bing.com`，后者有效则正常入账
    且不触发改写；两台都失败分别按 `no_results`/`serp_blocked` 结算；
  - `materials_found` 任务因容量被修剪时进入 `archived_tasks`，查询/来源
    快照可恢复，`archived_pending()`/`focus()` 可发现；归档超限写淘汰日志；
  - `title_excerpt_sha256` 只覆盖标题+摘要，不再冒充网页正文哈希；
  - 任务上限、尝试上限、配置变更只记一次；
  - 无概念支撑的最近词（日记尾词/文件名）不能变成搜索查询；
  - 工作记忆只读、决策加权、Teacherless 零痕迹等集成行为。

### 隔离冒烟 v9（全链路审计补丁后代码）

`runs/teacherless_smoke_20261026_v9/`（本地临时目录，不提交）：

- `from_zero` 初始化，代码版本 `9.24.5`，代码指纹
  `b6c924b5a808f532fd1a33baeae1b12076b15ce6ff60227d58c3d6c298f5ad76`；
- 3 轮（主动作 `self_talk` → `tidy_notes` → `tidy_notes`；辅助动作依次出现
  `reflect_memory`/`rest`/`self_talk`/`metacognition`/`consolidate_memory`/
  `build_site`）；
  `status.clean=true`、`code_drift=false`、`seed_problems=[]`、
  `code_migrations=[]`、`violations={}`；
- 8 项教师/LLM 痕迹全 0；`events.jsonl` 无框架级 error。第 3 轮辅助动作
  `build_site` 在本机启动网站服务失败（`[WinError 2]`，缺少启动器），
  属环境限制，不影响 Teacherless 隔离与研究账本；
- `state.research.schema_version=3`，任务/查询/来源/归档/落空补录均为 0，
  `research.stats` 全 0，`research.jsonl` 只有 `config_recorded`
  （`research-config-v3`）；
- 3 轮后 0 概念、0 命题、0 关系、0 预测，没有满足任务生成条件的缺口——
  这是正确行为，不是失败。若强行搜索，只会得到无来源主题的随机词，
  正是阶段 2 要避免的。

v6（`9.24.2`/`b3a2968d30fa`）、v7（`9.24.3`/`8bc7366a1a23`）与
v8（`9.24.4`/`b252df486490`）作为前几轮隔离证据保留在
`EXPERIMENT_LOG.md`。v9 只证明全链路审计补丁没有破坏隔离边界；它使用全新
状态，不能验证旧状态迁移、历史合并与队列拥挤时序——这些由
`tests/test_research.py` 的合成状态回归和
`runs/migration_check_20261026_v3/` 的 v1/v2 合成迁移验证覆盖。

### 合成 v1/v2 迁移验证

`runs/migration_check_20261026_v3/check.py`（独立脚本，只使用临时目录；
不读取、不写入仓库根目录或火星实时 `state.json`）：

- 最终代码指纹
  `b6c924b5a808f532fd1a33baeae1b12076b15ce6ff60227d58c3d6c298f5ad76`；
- `v1`：无 `schema_version` 的旧关系任务 → v3，ID 归一、查询/来源
  `task_id` 重映射、`content_sha256` 改名；同种类 `planned` 查询与尝试保留；
- `v2_repair`：归一化 ID、`schema_version=2`、两个尝试、旧种类计划查询 →
  归档旧尝试、重置尝试/改写、取消旧查询并标 `superseded`、同文本可重新规划；
  重复执行幂等；
- `v2_carrier`：载体换种类 → 旧尝试归档、计划查询取消、已执行查询与来源保留、
  新种类查询可规划，`task_id_migrated` 的归档/保留计数对账；
- 三个案例均 `ok: true`，报告写入同目录 `report.json`（`runs/` 不提交）。

### 真实搜索链路验证

`runs/research_harness_20261025/`（本地临时目录；合成一条“光|影”弱关系，
**不是学习实验**）：

- 任务来自 `relation_unverified`，查询带任务/目的/选择理由；
- 连续 5 次执行中 3 次抓到 5 条候选材料；
- Bing 跳转链已还原为 zhihu/zhidao 原站 URL；
- 来源记录含 task_id/query_id/purpose/serp_url/fetched_at/title_excerpt_sha256
  （2.1 前的记录字段名为 content_sha256，语义相同且从未代表正文）；
- 同时观测到：Bing 会偶尔返回首页/反爬页（记为 `serp_blocked`），
  且返回结果可能与查询主题无关——因此**搜索成功绝不能当成知识已验证**，
  这正是阶段 3 证据门禁要解决的问题。

## 11. 已知限制

1. **候选判定不是事实核验**：阶段 3.0 已实现正文提取与确定性线索判定，但规则只是
   “目标词共现 + 方向线索”，可能误判反讽、条件句或否定范围；没有来源权威性、
   发布时间、作者可信度模型。`candidate_support` 不是“已证实”。
2. **不验证、不晋升**：阶段 3.0 只写候选账本，不写 `claims`/`evidence`/
   `relations`/`laws`，不改任务状态，不把来源从 `unverified` 升级。
3. **验证器未接主循环**：`verify_task()`/`verify_pending()` 目前是可调用 API，
   没有自动触发、预算调度或重试策略；是否接线、如何限流留待后续阶段。
4. **查询模板仍是规则式**：只按任务类型分目的，没有查询效果学习或 bandit。
5. **Bing 可用性不稳定**：已做主机回退与失败分类，但不能保证结果相关性。
6. **任务来源范围有限**：只覆盖预测落空、命题证据缺口、弱关系、孤立概念；
   尚未覆盖“能力缺口/失败动作/矛盾集合”等更丰富信号。
7. **配置指纹范围有限**：阶段 2 的配置快照只覆盖研究与搜索参数；阶段 3.0 另有
   `verification.config_snapshot()`，但仍不是全运行配置快照。
8. **闭合任务的重新打开规则有限**：只有同一关系出现更强的 `prediction_miss`
   信号时才会重开/升级；同强度或更弱信号、以及其他类型的闭合任务不会自动重启，
   何时因新证据重开需要后续阶段设计。
9. **旧状态迁移只覆盖已知格式**：只迁移阶段 2.0 的关系任务 ID 与来源字段；
   更早/未知的研究 schema 不做猜测性修复。迁移只在 `ensure_state()` 首次运行，
   历史 JSONL 保持原样。碰撞合并的生命周期规则只处理当前已知的
   `open`/`active`/`materials_found`/`exhausted`/`blocked`/`abandoned` 组合；
   它保留可执行性与待验证入口，不推断候选材料是否真的支持命题。
   合并后的 `attempt_history` 只做有界保留（按轮次最近 5 条），更早条目不会
   迁入主任务，但迁入与截断数量会写入迁移日志，便于审计。
10. **抓取范围有限**：只接受 `text/html`、`application/xhtml+xml`、`text/plain`
    与 `identity` 编码；不执行 JavaScript、不解析 PDF/Office，正文超过 2 MB
    或文本超过 20 万字符会截断。动态站点可能只得到空壳页面或明确失败。
11. **独立性分组是保守近似**：按最终落点域名的可注册域分组，同域不同文章只算
    一个来源；托管平台整站合并；跨域转载若正文不同仍可能被当成两个来源。
    同正文去重只在同一任务内比较。宁可少算独立证据，不把同源当独立。
12. **没有评估**：本阶段不能证明“搜索 + 候选验证让系统变强”。

## 12. 下一阶段门槛（阶段 3 后续，尚未启动）

- 统一验证器注册表：算术探针、程序测试、形式规则、网络证据各自声明适用范围，
  网络候选不再与可执行验证混为一谈；
- 可撤销知识更新：命题/关系边只能在独立证据通过后更新，保留来源、版本与回滚记录；
- 把候选验证接入主循环或显式实验调度，并记录预算、重试与失败退避；
- 用留出任务验证“有研究队列/验证管线”相对“无”的增量；
- 在此之前，任何“学会/进化”表述都不成立。

阶段 3.0 的抓取、判定、账本、独立性、测试与冒烟细节见
[PHASE3_VERIFICATION.md](PHASE3_VERIFICATION.md)。
