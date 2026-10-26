#!/usr/bin/env bash
# 通用 CI 入口：安装依赖 + Chromium 内核 + 运行测试。
# 用法：在任意 CI（GitHub Actions / GitLab CI / Jenkins）中执行 bash ci/run-tests.sh
set -euo pipefail
cd "$(dirname "$0")/.."

python -m pip install --upgrade pip
pip install -e ".[dev]"
python -m playwright install --with-deps chromium
python -m pytest -q
