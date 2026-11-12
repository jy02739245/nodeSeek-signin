#!/usr/bin/env bash
# 更新 .env 里的 NS_COOKIE：自动替换、设置权限，避免手改写坏配置。
# 用法：sudo bash deploy/set-cookie.sh
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="$APP_DIR/.env"
APP_USER="${APP_USER:-$(stat -c '%U' "$ENV_FILE" 2>/dev/null || true)}"

[[ $EUID -eq 0 ]] || {
  echo "请用 root 运行：sudo bash deploy/set-cookie.sh" >&2
  exit 1
}
[[ -f "$ENV_FILE" ]] || {
  echo "找不到 $ENV_FILE，请先运行 deploy/install.sh" >&2
  exit 1
}

read -rs -p "粘贴 NodeSeek Cookie（不回显，回车确认）: " COOKIE
echo
COOKIE="$(printf '%s' "$COOKIE" | tr -d '\r' | sed -E 's/^[[:space:]]*[Cc]ookie:[[:space:]]*//; s/[[:space:]]+$//')"
[[ -n "$COOKIE" ]] || {
  echo "Cookie 为空，已取消" >&2
  exit 1
}

ENV_FILE="$ENV_FILE" COOKIE="$COOKIE" python3 - <<'PY'
import os
import pathlib
import re

path = pathlib.Path(os.environ["ENV_FILE"])
cookie = os.environ["COOKIE"]
if "\n" in cookie:
    raise SystemExit("Cookie 不能包含换行；多账号请用 & 连接")

new_line = f"NS_COOKIE={cookie}"
lines = path.read_text(encoding="utf-8").splitlines()
out = []
replaced = False
for existing in lines:
    if re.match(r"^\s*(export\s+)?NS_COOKIE\s*=", existing):
        out.append(new_line)
        replaced = True
    else:
        out.append(existing)
if not replaced:
    out.append(new_line)
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

chmod 600 "$ENV_FILE"
if [[ -n "$APP_USER" ]] && id -u "$APP_USER" >/dev/null 2>&1; then
  chown "$APP_USER:$(id -gn "$APP_USER")" "$ENV_FILE"
fi

echo "NS_COOKIE 已更新（${#COOKIE} 字符）。"
echo "配置文件：$ENV_FILE"
echo "手动验证：sudo systemctl start nodeseek-signin.service"
echo "查看日志：journalctl -u nodeseek-signin.service -n 50 --no-pager"
