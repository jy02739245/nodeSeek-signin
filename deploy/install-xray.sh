#!/usr/bin/env bash
# 安装固定版本的 Xray（仅 xray 二进制，不注册服务）。
# 用法：sudo bash deploy/install-xray.sh
# 可用 XRAY_VERSION=v26.11.9 覆盖版本。
set -euo pipefail

XRAY_VERSION="${XRAY_VERSION:-v26.11.9}"
TARGET="/usr/local/bin/xray"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash deploy/install-xray.sh"

case "$(uname -m)" in
  x86_64|amd64) ASSET="Xray-linux-64.zip" ;;
  aarch64|arm64) ASSET="Xray-linux-arm64-v8a.zip" ;;
  *) die "不支持的架构：$(uname -m)" ;;
esac

if ! command -v curl >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "缺少 curl，请先安装"
  log "安装 curl"
  apt-get update -qq
  apt-get install -y -qq curl
fi

if ! command -v unzip >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "缺少 unzip，请先安装"
  log "安装 unzip"
  apt-get update -qq
  apt-get install -y -qq unzip
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
base="https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION"

log "下载 Xray $XRAY_VERSION（$ASSET）"
curl -fsSL --retry 3 --connect-timeout 15 -o "$tmp/$ASSET" "$base/$ASSET"
curl -fsSL --retry 3 --connect-timeout 15 -o "$tmp/$ASSET.dgst" "$base/$ASSET.dgst"

expected="$(awk -F'= ' '/^SHA2-256=/{print $2}' "$tmp/$ASSET.dgst")"
[[ -n "$expected" ]] || die "无法从 .dgst 读取 SHA2-256"

if command -v sha256sum >/dev/null 2>&1; then
  echo "$expected  $tmp/$ASSET" | sha256sum -c -
else
  echo "$expected  $tmp/$ASSET" | shasum -a 256 -c -
fi

unzip -oq "$tmp/$ASSET" xray -d "$tmp"
install -m 0755 "$tmp/xray" "$TARGET"

log "已安装到 $TARGET"
"$TARGET" version | head -n 1
