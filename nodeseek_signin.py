#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

from curl_cffi import requests


BASE_URL = "https://www.nodeseek.com"
SIGN_IN_URL = f"{BASE_URL}/signIn.html"
ATTENDANCE_URL = f"{BASE_URL}/api/attendance"
LOGIN_URL = f"{BASE_URL}/api/account/signIn"
TURNSTILE_SITE_KEY = "0x4AAAAAAAaNy7leGjewpVyR"
GITHUB_API_VERSION = "2022-11-28"
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


@dataclass
class Account:
    index: int
    user: str
    password: str


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def split_cookies(raw_value: str) -> list[str]:
    if not raw_value:
        return []

    cookies: list[str] = []
    for part in re.split(r"(?:\r?\n|&)+", raw_value):
        cookie = part.strip().strip("\"'")
        if cookie.lower().startswith("cookie:"):
            cookie = cookie.split(":", 1)[1].strip()
        if cookie:
            cookies.append(cookie)
    return cookies


def collect_accounts() -> list[Account]:
    accounts: list[Account] = []
    base_user = os.getenv("USER", "").strip()
    base_password = os.getenv("PASS", "").strip()
    if base_user and base_password:
        accounts.append(Account(1, base_user, base_password))

    max_accounts = int(os.getenv("MAX_ACCOUNTS", "10"))
    for index in range(1, max_accounts + 1):
        user = os.getenv(f"USER{index}", "").strip()
        password = os.getenv(f"PASS{index}", "").strip()
        if user and password:
            accounts.append(Account(index, user, password))
    return accounts


def parse_gained(message: str) -> int:
    match = re.search(r"获得\s*(\d+)\s*鸡腿", message)
    if not match:
        match = re.search(r"(\d+)\s*鸡腿", message)
    return int(match.group(1)) if match else 0


def shorten_response(text: str, limit: int = 300) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


def request_timeout() -> int:
    return int(os.getenv("NS_TIMEOUT", "60"))


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


def should_retry_with_login(result: SignResult) -> bool:
    return result.status in {"invalid_cookie", "forbidden", "bad_response", "failed", "missing_cookie"}


def cf_solver_endpoint() -> str:
    solver_url = os.getenv("CF_SOLVER_URL", "").strip().rstrip("/")
    if not solver_url:
        return ""
    if solver_url.endswith("/cf-clearance-scraper"):
        return solver_url
    return f"{solver_url}/cf-clearance-scraper"


def solve_turnstile_token() -> tuple[str | None, str]:
    endpoint = cf_solver_endpoint()
    if not endpoint:
        return None, "未配置 CF_SOLVER_URL"

    mode = os.getenv("CF_SOLVER_MODE", "turnstile-min").strip() or "turnstile-min"
    payload: dict[str, object] = {
        "url": SIGN_IN_URL,
        "mode": mode,
    }
    if mode == "turnstile-min":
        payload["siteKey"] = TURNSTILE_SITE_KEY

    auth_token = os.getenv("CF_SOLVER_AUTH_TOKEN", "").strip()
    if auth_token:
        payload["authToken"] = auth_token

    try:
        response = requests.post(endpoint, json=payload, timeout=request_timeout())
        data = response.json()
    except Exception as exc:
        return None, f"请求验证码服务失败: {exc}"

    token = data.get("token") if isinstance(data, dict) else None
    if response.status_code == 200 and token:
        return str(token), "验证码解析成功"

    message = data.get("message") if isinstance(data, dict) else response.text
    return None, f"验证码解析失败，HTTP {response.status_code}: {message}"


def login_with_password(account: Account) -> tuple[str | None, str]:
    token, token_message = solve_turnstile_token()
    if not token:
        return None, token_message

    session = requests.Session(impersonate=impersonate_version())
    common_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE_URL,
        "Referer": SIGN_IN_URL,
    }

    try:
        session.get(SIGN_IN_URL, headers=common_headers, timeout=request_timeout())
        response = session.post(
            LOGIN_URL,
            json={
                "username": account.user,
                "password": account.password,
                "token": token,
                "source": "turnstile",
            },
            headers={**common_headers, "Content-Type": "application/json"},
            timeout=request_timeout(),
        )
        data = response.json()
    except Exception as exc:
        return None, f"登录请求失败: {exc}"

    if not data.get("success"):
        return None, str(data.get("message") or data)

    cookie_items = session.cookies.get_dict()
    cookie = "; ".join(f"{key}={value}" for key, value in cookie_items.items())
    if not cookie:
        return None, "登录成功但未获取到 Cookie"
    return cookie, "登录成功并获取到新 Cookie"


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def encrypt_for_github_secret(public_key_value: str, secret_value: str) -> str:
    import base64
    from nacl import encoding, public

    public_key = public.PublicKey(public_key_value.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def save_cookie_to_github_secret(cookie_value: str) -> bool:
    if not env_bool("SAVE_COOKIE_TO_GITHUB", True):
        print("SAVE_COOKIE_TO_GITHUB=false，跳过 GitHub Secret 写回")
        return True

    token = os.getenv("GH_PAT", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    secret_name = os.getenv("GITHUB_COOKIE_SECRET_NAME", "NS_COOKIE").strip() or "NS_COOKIE"
    if not token or not repo:
        print(f"GH_PAT 或 GITHUB_REPOSITORY 未配置，跳过 {secret_name} Secret 自动更新")
        return False

    api_base = f"https://api.github.com/repos/{repo}/actions/secrets"
    headers = github_headers(token)
    try:
        key_response = requests.get(f"{api_base}/public-key", headers=headers, timeout=30)
        key_response.raise_for_status()
        key_data = key_response.json()

        encrypted_value = encrypt_for_github_secret(key_data["key"], cookie_value)
        update_response = requests.put(
            f"{api_base}/{secret_name}",
            headers=headers,
            json={
                "encrypted_value": encrypted_value,
                "key_id": key_data["key_id"],
            },
            timeout=30,
        )
    except Exception as exc:
        print(f"更新 GitHub Secret {secret_name} 失败: {exc}")
        return False

    if update_response.status_code in {201, 204}:
        print(f"GitHub Secret {secret_name} 已更新，下次 Actions 运行会使用新 Cookie")
        return True

    print(f"更新 GitHub Secret {secret_name} 失败: HTTP {update_response.status_code} {update_response.text}")
    return False


def build_report(results: Iterable[SignResult]) -> str:
    result_list = list(results)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    success_count = sum(1 for result in result_list if result.ok)
    lines = [
        "NodeSeek 签到报告",
        f"时间: {now} Asia/Shanghai",
        f"结果: {success_count}/{len(result_list)} 成功",
        "",
    ]

    for result in result_list:
        mark = "OK" if result.ok else "FAIL"
        gained = f"，获得 {result.gained} 鸡腿" if result.gained else ""
        lines.append(f"[{mark}] 账号 {result.index}: {result.message}{gained}")

    return "\n".join(lines)


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


def main() -> int:
    raw_cookies = os.getenv("NS_COOKIE") or os.getenv("NS_COOKIES") or ""
    cookies = split_cookies(raw_cookies)
    accounts = collect_accounts()
    if not cookies and not accounts:
        print("未配置 NS_COOKIE，也未配置 USER/PASS。请至少配置其中一种方式。")
        return 2

    random_enabled = env_bool("NS_RANDOM", True)
    fail_on_error = env_bool("FAIL_ON_ERROR", True)
    max_count = max(len(cookies), len(accounts))
    while len(cookies) < max_count:
        cookies.append("")

    print(f"发现 {len([cookie for cookie in cookies if cookie])} 个 Cookie，{len(accounts)} 组账号密码，随机签到: {str(random_enabled).lower()}")
    results: list[SignResult] = []
    cookies_updated = False
    cookie_save_failed = False

    for index in range(1, max_count + 1):
        cookie = cookies[index - 1]
        account = accounts[index - 1] if index - 1 < len(accounts) else None
        print(f"\n开始签到账号 {index}")
        result = sign_cookie(cookie, index, random_enabled) if cookie else SignResult(index, False, "missing_cookie", "无 Cookie")

        if result.ok:
            print(f"账号 {index} [{result.status}]: {result.message}")
        else:
            print(f"账号 {index} [{result.status}]: {result.message}")
            if account and should_retry_with_login(result):
                print(f"账号 {index} 尝试使用账号密码和 Turnstile 自动登录")
                new_cookie, login_message = login_with_password(account)
                if new_cookie:
                    cookies[index - 1] = new_cookie
                    cookies_updated = True
                    print(f"账号 {index} {login_message}，重新签到")
                    result = sign_cookie(new_cookie, index, random_enabled)
                    print(f"账号 {index} [{result.status}]: {result.message}")
                else:
                    result = SignResult(index, False, "login_failed", login_message)
                    print(f"账号 {index} 自动登录失败: {login_message}")
            elif should_retry_with_login(result):
                print(f"账号 {index} 未配置对应 USER/PASS，无法自动登录刷新 Cookie")

        results.append(result)

    report = build_report(results)
    print("\n" + report)
    send_telegram(report)

    if cookies_updated:
        merged_cookie = "&".join(cookie for cookie in cookies if cookie)
        print("\n检测到 Cookie 更新，准备写回 GitHub Secret NS_COOKIE")
        cookie_save_failed = not save_cookie_to_github_secret(merged_cookie)

    has_failure = any(not result.ok for result in results)
    if fail_on_error and (has_failure or cookie_save_failed):
        print("\n存在签到失败账号或 Cookie 写回失败，任务标记为失败。若不希望失败退出，可设置变量 FAIL_ON_ERROR=false。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
