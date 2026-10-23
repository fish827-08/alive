# alive — SelfDrive 小脑自驱力实验（agent_v9）

无 LLM 的符号图自驱 agent：不使用任何大模型，纯 Python + 统计关系图（concepts/relations/rules/transforms）实现探索、记忆、提问、推理与情绪节律。运行在云服务器（qingwu@49.232.33.59）tmux 会话 `selfdrive` 中，由"大脑"（青梧，cron 桥接）通过 `brain/inbox.md` ↔ `brain/outbox/` 回答问题与主动投喂。

## 版本

- **agent_v9.py（v9.11 当前）**：数字加法句式识别——识别"X加Y加…等于Z"（中文数字），注册数字原子概念 + transforms 变换边（表达式→结果）+ 表达式/结果结构边。备份链 `.bak.v91`~`.bak.v911` 完整保留演进史。
- 历史主版本：`agent.py`（v1 前身）~ `agent_v8.py`。

## 目录结构

```
agent.py / agent_v2~v9.py   各代主版本（v9 为当前）
agent_v9.py.bak.v9x          v9 演进备份链
semantics.py                 语义相关模块
brain/                       大脑接口：inbox.md（问题）、outbox/（回答）、templates/（提问模板库）、ask_config.json、emotions.json
workspace/archive/           知识资料库（alife/ 人工生命与语言涌现、tech/ 信息论与天气等）
logs/  state.json            运行日志与状态（未入库，属运行时数据）
```

## 运行

```bash
cd ~/selfdrive && SELFDRIVE_INTERVAL=60 python3 -u agent_v9.py 2>&1 | tee -a logs/console.txt
```

（tmux 会话内运行；kb 记忆服务另起，端口 8100）

## 实验目标

观察符号图 agent 能否在"大脑"教学（投喂/答问/VERDICT 判定/模板引导）下自发涌现：提问→学用环→真概念推理→外围自我改进提案（"我想让…"）。
