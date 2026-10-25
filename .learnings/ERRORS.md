## [ERR-20261025-001] command_or_integration

**Logged**: 2026-10-25T16:57:55+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
Git Bash 环境没有 `python` 命令。

### Error
```
/usr/bin/bash: line 1: python: command not found
```

### Context
- 执行 `python -m compileall ...`。
- 工作区为 Windows，可用启动器是 `/c/WINDOWS/py`。

### Suggested Fix
本机统一使用 `py -m ...`，服务器再按环境使用 `python3`。

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-10-25T16:57:55+08:00
- **Notes**: 改用 `py` 后编译成功。

---

## [ERR-20261025-002] command_or_integration

**Logged**: 2026-10-25T16:57:55+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary
仓库没有可由 unittest 发现的测试。

### Error
```
Ran 0 tests in 0.000s
NO TESTS RAN
```

### Context
- 运行 `py -m unittest discover -v`。
- Python 编译检查已成功。

### Suggested Fix
为认知闭环、LLM 边界和状态持久化增加标准库 unittest 回归测试。

### Metadata
- Reproducible: yes
- Related Files: selfdrive/

### Resolution
- **Resolved**: 2026-10-25
- **Notes**: 新增 38 项标准库 unittest，覆盖状态原子写入、行动闭环、证据门禁、LLM 边界和已修缺陷。

---

## [ERR-20261025-003] command_or_integration

**Logged**: 2026-10-25
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
回归测试首轮 4 项失败，其中 2 项为实现问题、2 项为测试夹具问题。

### Error
```
ERROR: test_corrupt_state_is_not_silently_reset — FileExistsError
ERROR: test_pending_precedes_actual_and_brier_is_recorded — KeyError current_action
FAIL: test_undo_rebuilds_multiplier_from_remaining_rules — 3.0 != 6.0
FAIL: test_low_energy_prefers_rest_in_candidate_utility — rest < explore
```

### Context
- 运行 `py -m unittest discover -s tests -v`。
- 规则乘子受 3.0 上限夹紧；测试却假设会达到 6.0。
- 低能量时信息价值压过内稳态收益，属实现边界而非夹具错误。

### Suggested Fix
测试改用未触顶的乘子；提高内稳态在候选效用中的权重；测试夹具使用 `exist_ok=True` 并读取缺失键安全。

### Metadata
- Reproducible: yes
- Related Files: tests/, selfdrive/decision.py

### Resolution
- **Resolved**: 2026-10-25
- **Notes**: 规则测试改为 0.5×0.75；内稳态权重从 1.6 调至 12.0；随后 38 项全绿。

---
