#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from curl_cffi import requests

from nodeseek_proxy import ProxyError, maybe_launch


BASE_URL = "https://www.nodeseek.com"
ATTENDANCE_URL = f"{BASE_URL}/api/attendance"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class SignResult:
    index: int
    ok: bool
    status: str
    message: str
    gained: int = 0
    stats: SignStats | None = None


@dataclass
class SignStats:
    days_count: int
    total_amount: int
    average: float
    period: str


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_local_env() -> None:
    """加载脚本目录下的 .env；已存在的环境变量优先，GitHub Actions 不受影响。"""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"读取 {env_path} 失败: {exc}")
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def split_cookies(raw_value: str) -> list[str]:
    if not raw_value:
        return []

    raw_value = raw_value.strip()
    if not raw_value:
        return []

    if "&" in raw_value:
        parts = raw_value.split("&")
    else:
        parts = [part for part in raw_value.splitlines() if part.strip()]

    cookies: list[str] = []
    for part in parts:
        cookie = part.strip().strip("\"'")
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        if cookie:
            cookies.append(cookie)
    return cookies


def parse_gained(message: str) -> int:
    match = re.search(r"获得\s*(\d+)\s*鸡腿", message)
    if not match:
        match = re.search(r"(\d+)\s*鸡腿", message)
    return int(match.group(1)) if match else 0


def parse_credit_amount(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"-?\d+", str(value))
        return int(match.group(0)) if match else 0


def shorten_response(text: str, limit: int = 300) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


def request_timeout() -> int:
    return int(os.getenv("NS_TIMEOUT", "60"))


def request_proxies() -> dict[str, str] | None:
    """VPS IP 被 Cloudflare 拦截时，可通过 NS_PROXY 指定出口代理。"""
    proxy = os.getenv("NS_PROXY", "").strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def impersonate_version() -> str:
    return os.getenv("NS_IMPERSONATE", "chrome136")


def sign_cookie(cookie: str, index: int, random_enabled: bool) -> SignResult:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/board",
        "Cookie": cookie,
    }
    try:
        response = requests.post(
            ATTENDANCE_URL,
            params={"random": str(random_enabled).lower()},
            headers=headers,
            timeout=request_timeout(),
            impersonate=impersonate_version(),
            proxies=request_proxies(),
        )
    except Exception as exc:
        return SignResult(index, False, "request_error", f"请求异常: {exc}")

    if response.status_code == 403:
        return SignResult(index, False, "forbidden", "403 Forbidden，可能被 Cloudflare 拦截或 Cookie 已失效")

    try:
        data = response.json()
    except Exception:
        body = shorten_response(response.text)
        return SignResult(index, False, "bad_response", f"非 JSON 响应，HTTP {response.status_code}: {body}")

    message = str(data.get("message") or data.get("msg") or data)
    already_done = "已完成签到" in message or "已经签到" in message or "今日已签到" in message
    success = bool(data.get("success")) or already_done or ("鸡腿" in message and "失败" not in message)

    if success:
        status = "already" if already_done else "success"
        return SignResult(index, True, status, message, parse_gained(message))

    data_status = str(data.get("status"))
    if data_status in {"401", "403", "404"}:
        status = "invalid_cookie"
    else:
        status = "failed"
    return SignResult(index, False, status, message)


def get_signin_stats(cookie: str, days: int = 30) -> SignStats | None:
    if not cookie:
        return None

    shanghai_tz = ZoneInfo("Asia/Shanghai")
    days = max(days, 1)
    today = datetime.now(shanghai_tz).date()
    start_date = today - timedelta(days=days - 1)
    query_start = datetime.combine(start_date, time.min, tzinfo=shanghai_tz)
    signin_amounts_by_date: dict[str, int] = {}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE_URL}/board",
        "Cookie": cookie,
    }

    try:
        for page in range(1, 21):
            response = requests.get(
                f"{BASE_URL}/api/account/credit/page-{page}",
                headers=headers,
                timeout=request_timeout(),
                impersonate=impersonate_version(),
                proxies=request_proxies(),
            )
            data = response.json()
            if not isinstance(data, dict):
                break
            records = data.get("data")
            if not data.get("success") or not records:
                break

            should_stop = False
            for record in records:
                if len(record) < 4:
                    continue

                amount, _, description, timestamp = record[:4]
                record_time = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).astimezone(shanghai_tz)
                if record_time < query_start:
                    should_stop = True
                    continue

                if "签到收益" in str(description) and "鸡腿" in str(description):
                    record_date = record_time.strftime("%Y-%m-%d")
                    signin_amounts_by_date[record_date] = signin_amounts_by_date.get(record_date, 0) + parse_credit_amount(amount)

            if should_stop:
                break
    except Exception as exc:
        print(f"签到收益统计查询失败: {exc}")
        return None

    total_amount = sum(signin_amounts_by_date.values())
    days_count = len(signin_amounts_by_date)
    average = round(total_amount / days_count, 2) if days_count else 0
    return SignStats(
        days_count=days_count,
        total_amount=total_amount,
        average=average,
        period=f"近{days}天",
    )


def format_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def build_report(results: Iterable[SignResult]) -> str:
    result_list = list(results)
    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    success_count = sum(1 for result in result_list if result.ok)
    lines = [
        "NodeSeek 签到",
        "",
        "🤖 NodeSeek 自动签到报告",
        f"📅 日期: {today}",
        f"📊 统计: 成功 {success_count}/{len(result_list)}",
        "",
    ]

    for result in result_list:
        lines.append(f"账号：账号{result.index}")

        if result.ok:
            if result.gained or "鸡腿" in result.message or "收益" in result.message:
                lines.append(f"✅ 签到成功！您获得了 {result.message}")
            else:
                lines.append(f"✅ 签到成功！{result.message}")

            if result.stats:
                lines.extend(
                    [
                        f"签到收益统计（{result.stats.period}）",
                        f"📅 签到天数： {result.stats.days_count} 天",
                        f"🍗 总收益： {result.stats.total_amount} 个",
                        f"📈 日均收益： {format_number(result.stats.average)} 个",
                    ]
                )
        else:
            lines.append(f"❌ 签到失败：{result.message}")

        lines.append("")

    return "\n".join(lines).rstrip()


def send_telegram(text: str) -> None:
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_USER_ID", "").strip()
    if not token or not chat_id:
        return

    api_host = (os.getenv("TG_API_HOST") or "https://api.telegram.org").rstrip("/")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    thread_id = os.getenv("TG_THREAD_ID", "").strip()
    if thread_id:
        payload["message_thread_id"] = int(thread_id) if thread_id.isdigit() else thread_id

    try:
        response = requests.post(
            f"{api_host}/bot{token}/sendMessage",
            json=payload,
            timeout=30,
            impersonate=impersonate_version(),
        )
        response.raise_for_status()
        print("Telegram 通知发送成功")
    except Exception as exc:
        print(f"Telegram 通知发送失败: {exc}")


def run_signin(cookies: list[str]) -> int:
    random_enabled = env_bool("NS_RANDOM", True)
    fail_on_error = env_bool("FAIL_ON_ERROR", True)

    print(f"发现 {len(cookies)} 个 Cookie，随机签到: {str(random_enabled).lower()}")
    results: list[SignResult] = []

    for index, cookie in enumerate(cookies, start=1):
        print(f"\n开始签到账号 {index}")
        result = sign_cookie(cookie, index, random_enabled)
        print(f"账号 {index} [{result.status}]: {result.message}")
        if result.ok:
            result.stats = get_signin_stats(cookie, 30)
        results.append(result)

    report = build_report(results)
    print("\n" + report)
    send_telegram(report)

    if fail_on_error and any(not result.ok for result in results):
        print("\n存在签到失败账号，任务标记为失败。可设置 FAIL_ON_ERROR=false 关闭。")
        return 1
    return 0


def main() -> int:
    load_local_env()

    cookies = split_cookies(os.getenv("NS_COOKIE") or os.getenv("NS_COOKIES") or "")
    if not cookies:
        print("未配置 NS_COOKIE。请把 NodeSeek 登录后的 Cookie 填到 NS_COOKIE。")
        return 2

    try:
        with maybe_launch() as node_proxy:
            previous_proxy = os.environ.get("NS_PROXY")
            if node_proxy:
                print(f"NODE_LINK 代理就绪: {node_proxy}")
                os.environ["NS_PROXY"] = node_proxy
            try:
                return run_signin(cookies)
            finally:
                if previous_proxy is None:
                    os.environ.pop("NS_PROXY", None)
                else:
                    os.environ["NS_PROXY"] = previous_proxy
    except ProxyError as exc:
        print(f"NODE_LINK 启动失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
