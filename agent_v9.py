#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfdrive agent v9.23 — 模块化启动器。

全部逻辑在 selfdrive/ 包内，本文件只负责启动。
运行方式：python agent_v9.py [--once]，间隔 SELFDRIVE_INTERVAL。"""
from selfdrive.main import main

if __name__ == "__main__":
    main()
