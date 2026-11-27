#!/usr/bin/env bash
# 纯净宿主入口：用进程环境覆写模型配置后启动；不安装、不注入任何探针。
set -euo pipefail

DATA_DIR="${JIUWENSWARM_DATA_DIR:-$HOME/.jiuwenswarm}"
ENV_FILE="$DATA_DIR/config/.env"

# 模型变量以 -e 传入时覆写宿主自己的 .env（宿主以 override=True 加载该文件）。
if [ -f "$ENV_FILE" ]; then
  python3 - "$ENV_FILE" <<'PY'
import os
import re
import sys

KEYS = ("API_BASE", "API_KEY", "MODEL_NAME", "MODEL_PROVIDER", "CUSTOM_HEADERS")
vals = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
if vals:
    path = sys.argv[1]

    def quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    out, seen = [], set()
    with open(path, encoding="utf-8") as fh:
        for line in fh.read().splitlines():
            match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if match and match.group(1) in vals:
                key = match.group(1)
                out.append(f"{key}={quote(vals[key])}")
                seen.add(key)
            else:
                out.append(line)
    out.extend(f"{k}={quote(v)}" for k, v in vals.items() if k not in seen)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
PY
fi

echo "[jiuwenswarm-entrypoint] workspace=${DATA_DIR} cmd=$*"
exec "$@"
