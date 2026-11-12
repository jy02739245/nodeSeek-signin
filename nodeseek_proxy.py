#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 VLESS + XHTTP 节点链接转成本地 HTTP 代理，只依赖 Xray。

用法：
    NODE_LINK='vless://...' python nodeseek_signin.py
    python nodeseek_proxy.py --link 'vless://...' --print-config
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, unquote, urlsplit

DEFAULT_PORT = 1082
READY_TIMEOUT_SECONDS = 20
OUTBOUND_TAG = "node"
XHTTP_TYPES = {"xhttp", "splithttp"}

NODE_LINK_ENVS = ("NS_NODE_LINK", "NODE_LINK")
PORT_ENV = "NS_PROXY_PORT"
XRAY_PATH_ENV = "XRAY_PATH"


class ProxyError(RuntimeError):
    """节点链接无效，或 Xray 启动失败。"""


def node_link_from_env() -> Optional[str]:
    for name in NODE_LINK_ENVS:
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        for line in raw.replace(",", "\n").splitlines():
            link = line.strip().strip("\"'")
            if link:
                return link
    return None


def proxy_port() -> int:
    raw = os.getenv(PORT_ENV, "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"[node] {PORT_ENV}={raw} 不是数字，使用默认端口 {DEFAULT_PORT}")
        return DEFAULT_PORT


def _first(query: dict[str, list[str]], *names: str, default: str = "") -> str:
    for name in names:
        values = query.get(name)
        if values and values[0]:
            return unquote(values[0])
    return default


def _is_true(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _alpn_list(value: str) -> list[str]:
    return [item for item in (part.strip() for part in value.split(",")) if item]


def _network(query: dict[str, list[str]]) -> str:
    kind = _first(query, "type", "net").lower()
    if kind in XHTTP_TYPES:
        return "xhttp"
    if kind in ("", "tcp", "raw", "none"):
        raise ProxyError("只支持 VLESS + XHTTP，链接里需要 type=xhttp")
    raise ProxyError(f"只支持 VLESS + XHTTP，当前链接的 type={kind}")


def _xhttp_settings(query: dict[str, list[str]]) -> dict[str, Any]:
    _network(query)
    settings: dict[str, Any] = {}

    # v2rayN 会把 XHTTP 高级参数放在 extra=<urlencoded JSON>
    raw_extra = _first(query, "extra")
    if raw_extra:
        try:
            extra = json.loads(raw_extra)
        except json.JSONDecodeError as exc:
            raise ProxyError(f"xhttp extra 不是合法 JSON: {exc}") from exc
        if not isinstance(extra, dict):
            raise ProxyError("xhttp extra 必须是 JSON 对象")
        settings.update(extra)

    # 链接顶层的显式参数优先，缺省时保留 extra 里的值
    path = _first(query, "path")
    if path:
        settings["path"] = path
    settings.setdefault("path", "/")

    mode = _first(query, "mode").lower()
    if mode:
        settings["mode"] = mode
    settings.setdefault("mode", "auto")

    host = _first(query, "host")
    if host:
        settings["host"] = host

    return {"network": "xhttp", "xhttpSettings": settings}


def _security_settings(query: dict[str, list[str]], server: str) -> dict[str, Any]:
    security = _first(query, "security").lower()
    public_key = _first(query, "pbk", "public-key")

    if security == "reality" or public_key:
        if not public_key:
            raise ProxyError("REALITY 链接缺少 pbk（公钥）")
        return {
            "security": "reality",
            "realitySettings": {
                "serverName": _first(query, "sni", "peer", default=server),
                "fingerprint": _first(query, "fp", "client-fingerprint", default="chrome"),
                "publicKey": public_key,
                "shortId": _first(query, "sid", "short-id"),
                "spiderX": _first(query, "spx", "spider-x", default="/"),
            },
        }

    if security in ("tls", "xtls"):
        tls: dict[str, Any] = {
            "serverName": _first(query, "sni", "peer", default=server),
        }
        if _is_true(_first(query, "insecure", "allowInsecure", "allow_insecure")):
            tls["allowInsecure"] = True
        alpn = _alpn_list(_first(query, "alpn"))
        if alpn:
            tls["alpn"] = alpn
        fingerprint = _first(query, "fp", "client-fingerprint")
        if fingerprint:
            tls["fingerprint"] = fingerprint
        return {"security": "tls", "tlsSettings": tls}

    return {"security": "none"}


def parse_link(link: str) -> dict[str, Any]:
    if "://" not in link:
        raise ProxyError("节点链接缺少协议前缀")
    scheme, rest = link.split("://", 1)
    if scheme.lower() != "vless":
        raise ProxyError(f"只支持 vless://，当前是 {scheme}://")

    parts = urlsplit("//" + rest)
    if not parts.hostname:
        raise ProxyError("节点链接缺少服务器地址")
    if not parts.port:
        raise ProxyError("节点链接缺少端口")
    if not parts.username:
        raise ProxyError("节点链接缺少 UUID")

    query = parse_qs(parts.query)
    stream = {**_security_settings(query, parts.hostname), **_xhttp_settings(query)}
    return {
        "tag": OUTBOUND_TAG,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": parts.hostname,
                    "port": parts.port,
                    "users": [
                        {
                            "id": unquote(parts.username),
                            "encryption": _first(query, "encryption", default="none") or "none",
                        }
                    ],
                }
            ]
        },
        "streamSettings": stream,
    }


def build_config(link: str, port: int) -> dict[str, Any]:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "http",
                "settings": {},
            }
        ],
        "outbounds": [parse_link(link)],
    }


def find_xray() -> str:
    configured = os.getenv(XRAY_PATH_ENV, "").strip()
    if configured:
        if not os.path.exists(configured):
            raise ProxyError(f"{XRAY_PATH_ENV} 指向的文件不存在: {configured}")
        return configured

    for candidate in ("/usr/local/bin/xray", "/usr/bin/xray"):
        if os.path.exists(candidate):
            return candidate

    found = shutil.which("xray")
    if not found:
        raise ProxyError(
            "没找到 xray。先执行 sudo bash deploy/install-xray.sh，"
            "或设置 XRAY_PATH 指向 xray 可执行文件"
        )
    return found


def _wait_until_listening(port: int, process: subprocess.Popen) -> bool:
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _log_egress_ip(proxy_url: str) -> None:
    try:
        from curl_cffi import requests

        response = requests.get(
            "https://api.ip.sb/ip",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=15,
            impersonate="chrome",
        )
        text = response.text.strip()
        if response.ok and text:
            print(f"[node] 当前出口 IP: {text}")
        else:
            print(f"[node] 出口 IP 查询失败: HTTP {response.status_code}")
    except Exception as exc:
        print(f"[node] 出口 IP 查询失败: {exc}")


@contextmanager
def launch(link: str, port: Optional[int] = None) -> Iterator[str]:
    port = port or proxy_port()
    config = build_config(link, port)
    binary = find_xray()

    workdir = tempfile.mkdtemp(prefix="nodeseek-node-")
    config_path = Path(workdir) / "xray.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")

    outbound = config["outbounds"][0]
    target = outbound["settings"]["vnext"][0]
    print(f"[node] 启动 Xray: vless+xhttp → {target['address']}:{target['port']}，本地 127.0.0.1:{port}")

    try:
        process = subprocess.Popen(
            [binary, "run", "-c", str(config_path)],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise ProxyError(f"启动 Xray 失败: {exc}") from exc

    try:
        if not _wait_until_listening(port, process):
            output = ""
            if process.poll() is None:
                process.terminate()
            try:
                output = (process.communicate(timeout=3)[0] or "").strip()
            except subprocess.TimeoutExpired:
                process.kill()
            raise ProxyError(
                f"Xray {READY_TIMEOUT_SECONDS}s 内没有监听 127.0.0.1:{port}"
                + (f"；输出: {output[-800:]}" if output else "")
            )

        proxy_url = f"http://127.0.0.1:{port}"
        _log_egress_ip(proxy_url)
        yield proxy_url
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            print("[node] Xray 已退出")
        shutil.rmtree(workdir, ignore_errors=True)


@contextmanager
def maybe_launch() -> Iterator[Optional[str]]:
    link = node_link_from_env()
    if not link:
        yield None
        return
    with launch(link) as proxy_url:
        yield proxy_url


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="VLESS + XHTTP → 本地 HTTP 代理")
    parser.add_argument("--link", help="节点链接，默认读 NODE_LINK / NS_NODE_LINK")
    parser.add_argument("--port", type=int, default=None, help=f"本地端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--print-config", action="store_true", help="只打印 Xray 配置")
    args = parser.parse_args(argv)

    link = args.link or node_link_from_env()
    if not link:
        print("未配置 NODE_LINK")
        return 2

    try:
        config = build_config(link, args.port or proxy_port())
    except ProxyError as exc:
        print(f"链接解析失败: {exc}")
        return 1

    if args.print_config:
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return 0

    try:
        with launch(link, args.port):
            print("代理已启动，按 Ctrl+C 停止")
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        return 0
    except ProxyError as exc:
        print(f"启动失败: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
