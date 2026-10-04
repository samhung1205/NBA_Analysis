#!/usr/bin/env python
"""Production 重訓 / artifact 管理（C.5D）。見 core/production/retrain.py。

    python run_retrain.py                         # 重訓並 promote
    python run_retrain.py --cutoff 2026-10-03T00:00:00Z --no-promote
    python run_retrain.py --list
    python run_retrain.py --rollback [--to <artifact_version>]
"""
import sys

from core.production.retrain import main

if __name__ == "__main__":
    sys.exit(main())
