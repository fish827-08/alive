# Phase 2：研究任务队列与定向搜索规划

状态：**已实现第一小步并完成三轮审查修正及历史保留补丁，等待 owner 最后短复审。** 阶段 3
（自主验证闭环）、统一验证器、留出评估与策略优化均未启动。

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
| `selfdrive/__init__.py` | 版本 `9.24.4` |

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
- **预算只约束新建**：新建被拒后候选扫描继续，后面的既有任务仍可执行
  `_refresh_task()` / 原地升级；不会因为本轮额度用尽而跳过更高价值信号。
- **旧状态迁移**：`state.research.schema_version` 低于 2 时，`ensure_state()`
  执行一次显式迁移——把阶段 2.0 含 `kind` 的关系任务 ID 归一到关系键
  （`relation|A|B`），同步改写查询与来源索引里的 `task_id`，并把旧来源索引的
  `content_sha256` 更名为 `title_excerpt_sha256`。若同一关系同时存在新旧任务，
  合并为一条：任务种类取**最强信号**，生命周期取**仍可执行的那条**——
  有 `open`/`active` 任务时以它为准（`active` 优先，其次更新时间与优先级）；
  没有活跃任务但有候选材料时落为 `materials_found` 并标
  `pending_verification`；只有全部任务都已闭合时才保留
  `blocked`/`exhausted`/`abandoned` 的终止态。其他任务的当前尝试与已有
  `attempt_history` 一并迁入，按轮次排序后只保留最近 `HISTORY_KEEP=5` 条；
  超出时写 `attempt_history_truncated`（含 `dropped` 与 `keep`），
  `task_id_migrated` 同时记录 `attempt_history_imported`/`_dropped`。
  `kind_history` 记录种类切换，`query_ids`/`source_ids` 合并保留。迁移写
  `task_id_migrated` / `source_fields_migrated`；历史 JSONL 不重写。
  因此阶段 2.0 的实验状态可以继续使用，不需要伪装成“从零”状态；
  合并后的可执行任务仍在 `_viable_tasks()` 中，待验证材料仍能被
  `focus()`/阶段 3 验证入口发现。选择迁移而非强制重建：旧候选材料可继续被
  阶段 3 消费；若要做全新基线，仍应新建 `from_zero` 目录，二者不得混用。

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
2. 请求 Bing；`cn.bing.com` 传输失败或返回非结果页时回退 `www.bing.com`；
3. 解析 `b_algo` 结果；Bing 的 `/ck/a?...u=a1<base64>` 跳转链还原为真实 URL；
4. 成功时写 `workspace/web/web-*.md`（含任务 ID、查询 ID/目的、`unverified` 标记）；
5. 通过 `research.record_execution` 入账。

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

阶段 2 尚未实现正文提取，因此不存在 `page_content_sha256`；阶段 3 做来源变更
检测或证据去重时，不能把 `title_excerpt_sha256` 当成网页内容指纹。

`source_type=web_runtime`、`verification=unverified` 是硬编码值。阶段 2 不写
`claims.add_evidence`，不更新 `laws`，不因“多个网页说法相似”晋升任何知识。

## 8. 配置指纹

代码指纹只能覆盖 `.py` 文件，覆盖不到运行期配置。阶段 2 额外记录影响研究调度与
搜索策略的参数快照：

- 代码常量：任务/查询/尝试/改写/去重/历史上限，`RESEARCH_CONFIG_VERSION`；
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

- 全量：**142 项通过**（阶段 1.1 为 99 项，阶段 2/2.1 新增 43 项）。
- `tests/test_research.py`：43 项，覆盖 owner 指定的固定案例：
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
  - `attempt_history` 按轮次有界保留最近 5 条，截断写
    `attempt_history_truncated` 并记录迁入/丢弃数量；
  - `title_excerpt_sha256` 只覆盖标题+摘要，不再冒充网页正文哈希；
  - 任务上限、尝试上限、配置变更只记一次；
  - 无概念支撑的最近词（日记尾词/文件名）不能变成搜索查询；
  - 工作记忆只读、决策加权、Teacherless 零痕迹等集成行为。

### 隔离冒烟 v8（`attempt_history` 迁移补丁后代码）

`runs/teacherless_smoke_20261026_v8/`（本地临时目录，不提交）：

- `from_zero` 初始化，代码版本 `9.24.4`，代码指纹 `b252df486490`；
- 3 轮（主动作 `self_talk` → `self_talk` → `tidy_notes`）；
  `status.clean=true`、`code_drift=false`、`seed_problems=[]`、
  `code_migrations=[]`、`violations={}`；
- 8 项教师/LLM 痕迹全 0；`events.jsonl` 无框架级 error。第 2 轮辅助动作
  `build_site` 在本机启动网站服务失败（`[WinError 2]`，缺少启动器），
  属环境限制，不影响 Teacherless 隔离与研究账本；
- `state.research.schema_version=2`，任务/查询/来源均为 0，
  `research.stats` 全 0，`research.jsonl` 只有 `config_recorded`
  （`research-config-v2`）；
- 3 轮后 0 概念、0 命题、0 关系、0 预测，没有满足任务生成条件的缺口——
  这是正确行为，不是失败。若强行搜索，只会得到无来源主题的随机词，
  正是阶段 2 要避免的。

v6（`9.24.2`/`b3a2968d30fa`）与 v7（`9.24.3`/`8bc7366a1a23`）作为前两轮隔离
证据保留在 `EXPERIMENT_LOG.md`。v8 只证明隔离边界未被本次历史迁移补丁破坏；
它使用全新状态，不能验证旧状态迁移、历史合并与队列拥挤时序——这些由
`tests/test_research.py` 的合成状态回归覆盖。

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

1. **不做正文提取与证据判断**：只保存标题/摘要/URL；摘要可能误导。
   `title_excerpt_sha256` 只是摘要条目指纹，不是网页正文哈希。
2. **不验证、不晋升**：`materials_found` 之后没有消费者，阶段 3 才接验证器。
3. **查询模板仍是规则式**：只按任务类型分目的，没有查询效果学习或 bandit。
4. **Bing 可用性不稳定**：已做主机回退与失败分类，但不能保证结果相关性。
5. **任务来源范围有限**：只覆盖预测落空、命题证据缺口、弱关系、孤立概念；
   尚未覆盖“能力缺口/失败动作/矛盾集合”等更丰富信号。
6. **配置指纹范围有限**：只覆盖研究与搜索相关参数，不是全运行配置快照。
7. **闭合任务的重新打开规则有限**：只有同一关系出现更强的 `prediction_miss`
   信号时才会重开/升级；同强度或更弱信号、以及其他类型的闭合任务不会自动重启，
   何时因新证据重开需要后续阶段设计。
8. **旧状态迁移只覆盖已知格式**：只迁移阶段 2.0 的关系任务 ID 与来源字段；
   更早/未知的研究 schema 不做猜测性修复。迁移只在 `ensure_state()` 首次运行，
   历史 JSONL 保持原样。碰撞合并的生命周期规则只处理当前已知的
   `open`/`active`/`materials_found`/`exhausted`/`blocked`/`abandoned` 组合；
   它保留可执行性与待验证入口，不推断候选材料是否真的支持命题。
   合并后的 `attempt_history` 只做有界保留（按轮次最近 5 条），更早条目不会
   迁入主任务，但迁入与截断数量会写入迁移日志，便于审计。
9. **没有评估**：本阶段不能证明“搜索让系统变强”。

## 12. 下一阶段门槛（阶段 3，尚未启动）

- 将 `materials_found` 接到独立证据门禁：来源可信度、正文提取、同源去重、
  支持/反驳计数；
- 命题与关系边只能在独立证据通过后更新，且保留可撤销记录；
- 用留出任务验证“有研究队列”相对“无研究队列”的增量；
- 在此之前，任何“学会/进化”表述都不成立。
