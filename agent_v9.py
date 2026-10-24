#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfdrive agent 辉月 v9.16 — 模块化启动器。

全部逻辑在 selfdrive/ 包内（config/state/tokenizer/kb/emotions/templates/
predictions/learning/rules_compiler/decision/skills/main），本文件只负责启动。
运行方式与 v9.15 完全一致：python agent_v9.py [--once]，间隔 SELFDRIVE_INTERVAL。
历史版本备份：agent_v9.py.bak.v9152（重构前最后一版单文件）。"""
from selfdrive.main import main

if __name__ == "__main__":
    main()
