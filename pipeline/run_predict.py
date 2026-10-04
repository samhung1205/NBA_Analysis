#!/usr/bin/env python
"""Production 預測（C.5D）。見 core/production/predict.py。

    python run_predict.py --kind early            # 未來 36 小時比賽
    python run_predict.py --kind final            # 開賽前 5~75 分鐘、尚無 final 版本者
    python run_predict.py --kind refresh          # 有新傷病報告才重算
    python run_predict.py --kind early --dry-run  # 只計算不寫入
"""
import sys

from core.production.predict import main

if __name__ == "__main__":
    sys.exit(main())
