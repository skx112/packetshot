#!/usr/bin/env python3
"""兼容入口：没安装包时也可以直接 `python3 packetshot.py ...` 使用。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from packetshot.cli import main

if __name__ == "__main__":
    sys.exit(main())
