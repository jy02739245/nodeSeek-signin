#!/usr/bin/env bash
# 排查 403：启动 NODE_LINK 节点（如有），打印出口 IP、首页和签到接口响应。
# 用法：bash deploy/diagnose.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

[[ -x .venv/bin/python ]] || {
  echo "缺少 .venv，请先执行 sudo bash deploy/install.sh" >&2
  exit 1
}

.venv/bin/python - <<'PY'
import os
import sys

sys.path.insert(0, ".")
import nodeseek_signin as ns
from nodeseek_proxy import ProxyError, maybe_launch


def show(label, response):
    print(f"\n[{label}] HTTP {response.status_code}")
    for name in ("server", "cf-ray", "cf-mitigated", "content-type"):
        value = response.headers.get(name)
        if value:
            print(f"  {name}: {value}")
    body = " ".join(response.text.split())[:300]
    print(f"  body: {body}")


def run_checks(cookie):
    print(f"代理      : {os.getenv('NS_PROXY') or '(直连)'}")
    print(f"指纹      : {ns.impersonate_version()}")
    print(f"UA        : {ns.USER_AGENT}")
    print(f"Cookie    : {len(cookie)} 字符，cf_clearance={'cf_clearance=' in cookie}")

    for url in ("https://api.ip.sb/geoip", "https://ipinfo.io/json"):
        try:
            response = ns.requests.get(
                url,
                timeout=15,
                impersonate=ns.impersonate_version(),
                proxies=ns.request_proxies(),
            )
            print(f"\n[出口 IP] {response.text[:300]}")
            break
        except Exception as exc:
            print(f"\n[出口 IP] {url} 查询失败: {exc}")

    try:
        response = ns.requests.get(
            f"{ns.BASE_URL}/",
            timeout=20,
            impersonate=ns.impersonate_version(),
            proxies=ns.request_proxies(),
        )
        show("首页", response)
    except Exception as exc:
        print(f"[首页] 请求失败: {exc}")

    try:
        response = ns.requests.post(
            ns.ATTENDANCE_URL,
            params={"random": "true"},
            headers={
                "User-Agent": ns.USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": ns.BASE_URL,
                "Referer": f"{ns.BASE_URL}/board",
                "Cookie": cookie,
            },
            timeout=30,
            impersonate=ns.impersonate_version(),
            proxies=ns.request_proxies(),
        )
        show("签到 API", response)
    except Exception as exc:
        print(f"[签到 API] 请求失败: {exc}")


def main():
    ns.load_local_env()
    cookies = ns.split_cookies(os.getenv("NS_COOKIE", ""))
    if not cookies:
        print("未配置 NS_COOKIE")
        return 2

    try:
        with maybe_launch() as node_proxy:
            previous = os.environ.get("NS_PROXY")
            if node_proxy:
                print(f"NODE_LINK 代理: {node_proxy}")
                os.environ["NS_PROXY"] = node_proxy
            try:
                run_checks(cookies[0])
            finally:
                if previous is None:
                    os.environ.pop("NS_PROXY", None)
                else:
                    os.environ["NS_PROXY"] = previous
    except ProxyError as exc:
        print(f"NODE_LINK 启动失败: {exc}")
        return 1
    return 0


raise SystemExit(main())
PY
