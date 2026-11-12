#!/usr/bin/env bash
# 就地安装：配置(.env)和虚拟环境都放在项目目录，并注册 systemd 定时器。
# 用法：sudo bash deploy/install.sh [--with-xray]
set -euo pipefail

WITH_XRAY=0
for arg in "$@"; do
  case "$arg" in
    --with-xray) WITH_XRAY=1 ;;
    -h|--help)
      sed -n '2,3p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 2
      ;;
  esac
done

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_OWNER="$(stat -c '%U' "$APP_DIR" 2>/dev/null || echo root)"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-$DEFAULT_OWNER}}"
# root 直接跑时不要用 root 当服务用户
[[ "$SERVICE_USER" == "root" ]] && SERVICE_USER="nodeseek"
PYTHON_BIN="${PYTHON_BIN:-python3}"
# 想和其他项目共用依赖时，指向一个已存在的 venv，例如 /opt/shared-venv
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy/install.sh"

"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "$PYTHON_BIN 版本过低，需要 Python 3.10+"

if ! "$PYTHON_BIN" -c 'import venv' >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    log "安装系统依赖 python3-venv / python3-pip"
    apt-get update -qq
    apt-get install -y -qq python3-venv python3-pip
  else
    die "缺少 venv 模块，请先用系统包管理器安装 python3-venv"
  fi
fi

log "项目目录：$APP_DIR"
log "运行用户：$SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

if [[ "$VENV_DIR" == "$APP_DIR/.venv" ]]; then
  # 从别的机器同步过来的 venv 可能不可用，重建
  if [[ -e "$VENV_DIR" ]] && ! "$VENV_DIR/bin/python" -c 'import sys' >/dev/null 2>&1; then
    log "现有 .venv 不可用，重建"
    rm -rf "$VENV_DIR"
  fi
  log "创建独立虚拟环境并安装依赖"
  [[ -x "$VENV_DIR/bin/python" ]] || "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  log "使用共享虚拟环境 $VENV_DIR"
  [[ -x "$VENV_DIR/bin/python" ]] || die "$VENV_DIR 不存在，请先创建共享 venv，或去掉 VENV_DIR 使用独立环境"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip -q
"$VENV_DIR/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"

if [[ "$WITH_XRAY" == "1" ]]; then
  bash "$APP_DIR/deploy/install-xray.sh"
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  log "生成 .env（首次部署）"
  cp "$APP_DIR/deploy/env.example" "$APP_DIR/.env"
  ENV_CREATED=1
else
  ENV_CREATED=0
fi
chmod 600 "$APP_DIR/.env"

log "安装 systemd 服务与定时器"
sed -e "s|@APP_DIR@|$APP_DIR|g" \
    -e "s|@APP_USER@|$SERVICE_USER|g" \
    -e "s|@APP_GROUP@|$SERVICE_GROUP|g" \
    -e "s|@PYTHON@|$VENV_DIR/bin/python|g" \
    "$APP_DIR/deploy/nodeseek-signin.service" > /etc/systemd/system/nodeseek-signin.service
install -m 644 "$APP_DIR/deploy/nodeseek-signin.timer" /etc/systemd/system/nodeseek-signin.timer

# 只改运行所需文件的属主，不整目录 chown（避免动到 .git）
if [[ "$VENV_DIR" == "$APP_DIR/.venv" ]]; then
  chown -R "$SERVICE_USER:$SERVICE_GROUP" "$VENV_DIR"
fi
chown "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/.env"

if ! runuser -u "$SERVICE_USER" -- test -r "$APP_DIR/nodeseek_signin.py" 2>/dev/null; then
  die "服务用户 $SERVICE_USER 读不到 $APP_DIR。项目别放在 /root 这类目录下，或设置 SERVICE_USER 为可访问该目录的用户"
fi

systemctl daemon-reload

if [[ "$ENV_CREATED" == "1" ]]; then
  # 配置还是空的，先只 enable，避免启用瞬间就跑一次失败任务
  systemctl enable nodeseek-signin.timer >/dev/null
  log "已启用定时器（配置为空，暂未启动）"
  printf '\n下一步：\n  1. 执行 sudo bash %s/deploy/set-cookie.sh 写入 NS_COOKIE\n  2. 启动定时器：sudo systemctl start nodeseek-signin.timer\n  3. 手动跑一次：sudo systemctl start nodeseek-signin.service\n  4. 看日志：journalctl -u nodeseek-signin.service -n 100 --no-pager\n' "$APP_DIR"
else
  systemctl enable --now nodeseek-signin.timer >/dev/null
  log "已启用并启动定时器（保留现有 .env）"
  printf '\n手动测试：sudo systemctl start nodeseek-signin.service\n'
fi

systemctl list-timers nodeseek-signin.timer --no-pager || true
printf '\n配置：%s/.env\nPython：%s\n' "$APP_DIR" "$VENV_DIR/bin/python"
if [[ ! -x /usr/local/bin/xray ]] && ! command -v xray >/dev/null 2>&1; then
  printf '提示：配置 NODE_LINK 前先执行 sudo bash %s/deploy/install-xray.sh\n' "$APP_DIR"
fi
