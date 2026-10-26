# 数据快照 20261026（分析分支，勿合并回 main）

采集时间：2026-10-26 19:50（北京时间）。本分支为只读数据快照，基于 main@2500c4e（v9.24.5）切出，不包含此后的代码改动。

## 目录与证据等级
- `huiyue_selfdrive/`（辉月，云端 ~/selfdrive）
  - `brain/`：inbox 累计 638 题 + outbox 全部教师回答
  - `state.json`：rounds=15015，情绪/能力/关系全量（实测读取）
  - `logs/console.txt events.jsonl proposals.jsonl evidence.jsonl`：全量
  - `logs/action_outcomes.tail.jsonl`：仅末 5MB（原文件 24MB，为控体积截断）
- `moon/`（登月，云端 ~/moon）：结构同上；rounds=9318，q=106，read=85
- `mars/`（火星，本机 D:\Project\alive 工作树副本）
  - 停机时刻快照：round 2548（2026-10-25 22:35 owner 令停），教师回答 46 封未被读
  - `logs/`：console/events/proposals(5115条,全部 unverified)/evidence
  - 注意：火星 brain/inbox.md 内含 1826-1953 共 41 道未答题，其中多数从未被读取过回答（停机）

## 已知口径与坑
1. 两云端学生今日提问以《X》反面句式为主——该句式同时是 teacherless 模板种子（见 main 分支 runs 实验目录 brain/templates/seed-contrast.txt），分析"自发涌现"时须扣除模板遗传。
2. 残渣词（ensp/https/cache/哔哩/Wordscapes/百度 等）来自网页扫描，混入概念库属已知污染，非学生自生。
3. `runs/` 目录（teacherless 冒烟实验 v6-v9）未入本快照，属实验数据非学生数据，在 main 工作区可查。
4. 关系分布（co_occur/causal/contrast/...）以各自 state.json 内语义库为准；console 行为叙述文本证据等级低于 state 数值。
