#!/bin/sh
# 容器入口：可选启动前自检（RUN_TESTS_ON_START=1），随后启动 HTTP 服务。
set -e

if [ "${RUN_TESTS_ON_START:-0}" = "1" ]; then
    echo "[entrypoint] RUN_TESTS_ON_START=1，运行启动前自检..."
    pytest tests/ -q
fi

exec "$@"
