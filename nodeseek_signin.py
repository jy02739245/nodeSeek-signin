#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from curl_cffi import CurlMime, requests


def load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.is_file():
        return
    with env_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value


load_dotenv()


BASE_URL = "https://www.nodeseek.com"
SIGN_IN_URL = f"{BASE_URL}/signIn.html"
ATTENDANCE_URL = f"{BASE_URL}/api/attendance"
LOGIN_URL = f"{BASE_URL}/api/account/signIn"
LOGIN_UTIL_URL = f"{BASE_URL}/api/account/util"
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
    account_name: str = ""
    stats: SignStats | None = None


@dataclass
class SignStats:
    days_count: int
    total_amount: int
    average: float
    period: str


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
        cookies.append(cookie)
    return cookies


def join_cookies_for_secret(cookies: list[str]) -> str:
    normalized = [cookie.strip() for cookie in cookies]
    while normalized and not normalized[-1]:
        normalized.pop()
    return "&".join(normalized)


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


def impersonate_version() -> str:
    return os.getenv("NS_IMPERSONATE", "chrome136")


def auth_value_from_credential(credential: str) -> str:
    value = credential.strip()
    lower_value = value.lower()
    if lower_value.startswith("authorization:"):
        return value.split(":", 1)[1].strip()
    if lower_value.startswith("bearer "):
        return value

    for prefix in ("auth_token=", "access_token=", "token="):
        if lower_value.startswith(prefix):
            return f"Bearer {value.split('=', 1)[1].strip()}"

    if value.count(".") >= 2 and ";" not in value and "=" not in value:
        return f"Bearer {value}"

    return ""


def token_credential(token: str) -> str:
    token = token.strip()
    if token.lower().startswith("bearer "):
        return token
    return f"Bearer {token}"


def build_nodeseek_headers(credential: str, content_type: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{BASE_URL}/board",
    }
    if content_type:
        headers["Content-Type"] = "application/json"

    auth_value = auth_value_from_credential(credential)
    if auth_value:
        headers["Authorization"] = auth_value
    else:
        headers["Cookie"] = credential
    return headers


def sign_cookie(cookie: str, index: int, random_enabled: bool) -> SignResult:
    headers = build_nodeseek_headers(cookie)
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
        **build_nodeseek_headers(cookie),
    }

    try:
        for page in range(1, 21):
            response = requests.get(
                f"{BASE_URL}/api/account/credit/page-{page}",
                headers=headers,
                timeout=request_timeout(),
                impersonate=impersonate_version(),
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
        return solve_turnstile_with_browser()

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
    print(f"CF_SOLVER 验证码解析失败: {message}，尝试浏览器方案...")
    return solve_turnstile_with_browser()


def solve_turnstile_with_browser() -> tuple[str | None, str]:
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None, "patchright/playwright 未安装，无法使用浏览器解析验证码"

    headless = env_bool("NS_HEADLESS", False)
    print(f"启动浏览器解析 Turnstile 验证码 (headless={headless})...")

    try:
        with sync_playwright() as p:
            profile_dir = str(Path(__file__).parent / ".browser_profile")
            context = p.chromium.launch_persistent_context(
                profile_dir,
                headless=headless,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.new_page()
            page.goto(SIGN_IN_URL, wait_until="load", timeout=60000)
            page.wait_for_timeout(8000)

            _click_turnstile(page)

            for attempt in range(40):
                token_value = page.evaluate("""() => {
                    const input = document.querySelector('[name="cf-turnstile-response"]');
                    return (input && input.value) ? input.value : '';
                }""")
                if token_value:
                    context.close()
                    print("浏览器 Turnstile 验证码解析成功")
                    return token_value, "验证码解析成功"
                if attempt % 10 == 9:
                    print(f"  等待 Turnstile 验证完成... ({attempt + 1}s)")
                page.wait_for_timeout(1000)

            context.close()
    except Exception as exc:
        return None, f"浏览器验证码解析异常: {exc}"

    return None, "浏览器未能解析 Turnstile 验证码（超时）"


def _click_turnstile(page):
    for frame in page.frames:
        if "challenges.cloudflare.com" in (frame.url or ""):
            try:
                el = frame.frame_element()
                bbox = el.bounding_box()
                if bbox:
                    page.mouse.click(bbox["x"] + 30, bbox["y"] + bbox["height"] / 2)
                    return
            except Exception:
                pass

    turnstile_container = page.evaluate("""() => {
        const el = document.querySelector('.cf-turnstile, [class*="turnstile"]');
        if (el) {
            const r = el.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        }
        const input = document.querySelector('[name="cf-turnstile-response"]');
        if (input && input.parentElement) {
            const r = input.parentElement.getBoundingClientRect();
            return {x: r.x, y: r.y, w: r.width, h: r.height};
        }
        return null;
    }""")
    if turnstile_container:
        page.mouse.click(
            turnstile_container["x"] + 30,
            turnstile_container["y"] + turnstile_container["h"] / 2,
        )
    else:
        page.mouse.click(155, 300)


NOISE_COOKIE_NAMES = {
    "_ga",
    "_gid",
    "_gat",
    "_cfuvid",
    "cf_clearance",
    "next_locale",
    "locale",
    "theme",
    "colorscheme",
}
NOISE_COOKIE_PREFIXES = ("_ga_", "__cf", "cf_")
AUTH_COOKIE_NAMES = {
    "fog",
    "hmti_",
    "pjwt",
    "session",
    "smac",
}


def nodeseek_cookies(context) -> list[dict]:
    try:
        cookies = context.cookies()
    except Exception:
        cookies = context.cookies([BASE_URL, "https://nodeseek.com"])

    seen: set[tuple[str, str, str]] = set()
    result: list[dict] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "")
        if "nodeseek.com" not in domain:
            continue
        key = (
            str(cookie.get("name") or ""),
            domain,
            str(cookie.get("path") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(cookie)
    return result


def cookie_name_list(cookies: list[dict]) -> list[str]:
    return sorted(str(cookie.get("name") or "") for cookie in cookies if cookie.get("name"))


def is_noise_cookie(cookie: dict) -> bool:
    name = str(cookie.get("name") or "").lower()
    return name in NOISE_COOKIE_NAMES or any(name.startswith(prefix) for prefix in NOISE_COOKIE_PREFIXES)


def is_auth_cookie(cookie: dict) -> bool:
    name = str(cookie.get("name") or "").lower()
    return name in AUTH_COOKIE_NAMES


def browser_error_message(page) -> str:
    try:
        return page.evaluate("""() => {
            const selectors = [
                '.alert',
                '.error',
                '[role="alert"]',
                '[class*="error"]',
                '[class*="alert"]',
                '[class*="toast"]'
            ];
            for (const selector of selectors) {
                for (const node of document.querySelectorAll(selector)) {
                    const text = (node.textContent || '').trim();
                    if (text) return text.substring(0, 200);
                }
            }
            return '';
        }""")
    except Exception:
        return ""


def cookies_to_header(cookies: list[dict]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name"))


def telegram_configured() -> bool:
    return bool(os.getenv("TG_BOT_TOKEN", "").strip() and os.getenv("TG_USER_ID", "").strip())


def save_browser_login_screenshot(page, account: Account, status: str) -> Path | None:
    if not telegram_configured():
        return None

    screenshot_dir = Path(__file__).parent / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    status_slug = {
        "成功": "success",
        "失败": "failure",
    }.get(status, status)
    safe_status = re.sub(r"[^a-z0-9_-]+", "-", status_slug.lower()).strip("-") or "state"
    path = screenshot_dir / f"nodeseek-browser-login-{safe_status}-account-{account.index}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception as exc:
        print(f"浏览器登录截图保存失败: {exc}")
        return None

    print(f"浏览器登录截图已保存: {path}")
    return path


def notify_browser_login_screenshot(page, account: Account, status: str, detail: str = "") -> None:
    screenshot_path = save_browser_login_screenshot(page, account, status)
    if not screenshot_path:
        return

    caption = f"NodeSeek 浏览器登录{status}：{mask_account_name(account.user)}"
    if detail:
        caption = f"{caption}\n{shorten_response(detail, 500)}"
    send_telegram_photo(screenshot_path, caption)


def auth_cookies(cookies: list[dict]) -> list[dict]:
    return [cookie for cookie in cookies if is_auth_cookie(cookie)]


def read_browser_auth_token(page) -> str:
    try:
        token = page.evaluate("""() => {
            const keys = [
                'auth_token',
                'access_token',
                'token',
                'jwt',
                'authorization'
            ];
            for (const storage of [localStorage, sessionStorage]) {
                for (const key of keys) {
                    const value = storage.getItem(key);
                    if (value) return value;
                }
            }
            return '';
        }""")
        return str(token or "").strip()
    except Exception:
        return ""


def safe_login_response_summary(response) -> str:
    status = getattr(response, "status", "?")
    try:
        text = response.text()
    except Exception as exc:
        return f"HTTP {status}, body_unavailable={type(exc).__name__}"

    try:
        data = json.loads(text)
    except Exception:
        masked = re.sub(
            r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{16,}(?:\.[A-Za-z0-9_-]{16,})?",
            "***",
            shorten_response(text, 180),
        )
        return f"HTTP {status}, text={masked}"

    if not isinstance(data, dict):
        return f"HTTP {status}, JSON={type(data).__name__}"

    keys = ", ".join(sorted(str(key) for key in data.keys()))
    message = data.get("message") or data.get("msg") or ""
    success = data.get("success")
    summary = f"HTTP {status}, success={success}, keys=[{keys}]"
    if message:
        summary += f", message={str(message)[:120]}"
    return summary


def click_visible_login_button(page) -> bool:
    try:
        return bool(page.evaluate("""() => {
            const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && rect.width > 0
                    && rect.height > 0;
            };
            const password = document.querySelector('#stacked-password')
                || document.querySelector('input[type="password"]');
            const form = password ? password.closest('form') : null;
            const scope = form || document;
            const candidates = Array.from(scope.querySelectorAll('button, input[type="submit"]'));
            const target = candidates.find((el) => {
                const text = (el.innerText || el.value || el.textContent || '').trim();
                return isVisible(el)
                    && !el.disabled
                    && !el.getAttribute('aria-disabled')
                    && text.includes('登录');
            });
            if (!target) return false;
            target.scrollIntoView({block: 'center', inline: 'center'});
            target.click();
            return true;
        }"""))
    except Exception:
        return False


def login_with_browser(account: Account) -> tuple[str | None, str]:
    try:
        from patchright.sync_api import sync_playwright
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None, "patchright/playwright 未安装，无法使用浏览器登录"

    headless = env_bool("NS_HEADLESS", False)
    print(f"启动浏览器执行完整登录流程 (headless={headless})...")

    try:
        with sync_playwright() as p:
            profile_dir = str(Path(__file__).parent / ".browser_profile")
            context = p.chromium.launch_persistent_context(
                profile_dir,
                headless=headless,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.new_page()
            page.goto(SIGN_IN_URL, wait_until="load", timeout=60000)
            page.wait_for_timeout(8000)

            page.locator("#stacked-email").fill(account.user)
            page.locator("#stacked-password").fill(account.password)
            print("  已填入用户名和密码")

            _click_turnstile(page)

            print("  等待 Turnstile 验证完成...")
            for attempt in range(40):
                token_value = page.evaluate("""() => {
                    const input = document.querySelector('[name="cf-turnstile-response"]');
                    return (input && input.value) ? input.value : '';
                }""")
                if token_value:
                    print("  Turnstile 验证通过")
                    break
                if attempt % 10 == 9:
                    print(f"  等待中... ({attempt + 1}s)")
                page.wait_for_timeout(1000)
            else:
                notify_browser_login_screenshot(page, account, "失败", "Turnstile 验证超时（40s）")
                context.close()
                return None, "Turnstile 验证超时（40s）"

            login_response_summary = ""
            clicked = False
            try:
                with page.expect_response(lambda response: response.url.startswith(LOGIN_URL), timeout=15000) as response_info:
                    clicked = click_visible_login_button(page)
                    if not clicked:
                        page.locator("#stacked-password").press("Enter")
                        clicked = True
                login_response = response_info.value
                login_response_summary = safe_login_response_summary(login_response)
                print(f"  登录 API 响应: {login_response_summary}")
            except Exception as exc:
                if not clicked:
                    clicked = click_visible_login_button(page)
                if not clicked:
                    notify_browser_login_screenshot(page, account, "失败", f"未找到可点击的登录按钮: {exc}")
                    context.close()
                    return None, f"未找到可点击的登录按钮: {exc}"
                print(f"  已点击登录按钮，未捕获到登录 API 响应: {exc}")

            print("  已点击登录按钮，等待登录态 Cookie...")

            cookies: list[dict] = []
            credential_cookies: list[dict] = []
            auth_token = ""
            for wait in range(45):
                page.wait_for_timeout(1000)
                auth_token = read_browser_auth_token(page)
                if auth_token:
                    break
                cookies = nodeseek_cookies(context)
                credential_cookies = auth_cookies(cookies)
                if credential_cookies:
                    break
                if wait in {9, 24, 39}:
                    names = ", ".join(cookie_name_list(cookies)) or "无"
                    print(f"  等待登录态 Cookie... 当前 NodeSeek cookie: {names}")
                error_msg = browser_error_message(page)
                if error_msg and wait >= 2:
                    notify_browser_login_screenshot(page, account, "失败", f"浏览器登录失败: {error_msg}")
                    context.close()
                    return None, f"浏览器登录失败: {error_msg}"
            else:
                names = ", ".join(cookie_name_list(cookies)) or "无"
                current_url = page.url
                expected = ", ".join(sorted(AUTH_COOKIE_NAMES))
                detail = f"未获取到有效登录 Token/Cookie；当前 URL: {current_url}；当前 NodeSeek cookie: {names}；期望包含: {expected}"
                notify_browser_login_screenshot(page, account, "失败", detail)
                context.close()
                return None, f"登录后未获取到有效登录 Token/Cookie（当前 URL: {current_url}，当前 NodeSeek cookie: {names}，期望包含: {expected}）"

            if auth_token:
                notify_browser_login_screenshot(page, account, "成功", "获取到前端登录 Token")
                context.close()
                print("  浏览器登录成功，获取到前端登录 Token")
                return token_credential(auth_token), "浏览器登录成功并获取到 Token"

            cookie_str = cookies_to_header(cookies)
            names = ", ".join(cookie_name_list(cookies)) or "无"
            credential_names = ", ".join(cookie_name_list(credential_cookies)) or "未知"

            if not cookie_str:
                notify_browser_login_screenshot(page, account, "失败", "浏览器登录成功但未获取到 Cookie")
                context.close()
                return None, "浏览器登录成功但未获取到 Cookie"
            print(f"  浏览器登录成功，获取到 {len(cookies)} 个 NodeSeek cookie: {names}")
            print(f"  识别到登录凭据 Cookie: {credential_names}")
            notify_browser_login_screenshot(page, account, "成功", f"识别到登录凭据 Cookie: {credential_names}")
            context.close()
            return cookie_str, "浏览器登录成功"
    except Exception as exc:
        return None, f"浏览器登录异常: {exc}"


def find_nested_value(value, normalized_names: set[str]) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in normalized_names and item:
                return str(item)
            found = find_nested_value(item, normalized_names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_nested_value(item, normalized_names)
            if found:
                return found
    return ""


def extract_login_token(data) -> str:
    return find_nested_value(
        data,
        {
            "token",
            "authtoken",
            "accesstoken",
            "authorization",
            "jwt",
        },
    ).strip()


def get_login_security_headers(session, common_headers: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    env_security_token = os.getenv("NS_SECURITY_TOKEN", "").strip()
    env_integrity_token = os.getenv("NS_INTEGRITY_TOKEN", "").strip()
    if env_security_token:
        headers["x-security-token"] = env_security_token
    if env_integrity_token:
        headers["x-integrity-token"] = env_integrity_token

    if headers:
        return headers

    try:
        response = session.get(
            LOGIN_UTIL_URL,
            headers=common_headers,
            timeout=request_timeout(),
        )
        data = response.json()
    except Exception as exc:
        print(f"登录安全 token 获取失败: {exc}")
        return headers

    security_token = find_nested_value(data, {"securitytoken", "xsecuritytoken"})
    integrity_token = find_nested_value(data, {"integritytoken", "xintegritytoken"})
    if security_token:
        headers["x-security-token"] = security_token
    if integrity_token:
        headers["x-integrity-token"] = integrity_token
    if headers:
        print("已获取登录安全 token")
    return headers


def _attempt_login(account: Account, captcha_token: str | None) -> tuple[str | None, str]:
    session = requests.Session(impersonate=impersonate_version())
    common_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": BASE_URL,
        "Referer": SIGN_IN_URL,
    }

    try:
        session.get(SIGN_IN_URL, headers=common_headers, timeout=request_timeout())
    except Exception as exc:
        return None, f"登录页请求失败: {exc}"

    security_headers = get_login_security_headers(session, common_headers)
    login_headers = {
        **common_headers,
        **security_headers,
        "Content-Type": "application/json",
    }
    if captcha_token:
        login_headers["x-captcha-source"] = "turnstile"
        login_headers["x-captcha-token"] = captcha_token

    try:
        response = session.post(
            LOGIN_URL,
            json={
                "username": account.user,
                "password": account.password,
            },
            headers=login_headers,
            timeout=request_timeout(),
        )
        data = response.json()
    except Exception as exc:
        return None, f"登录请求失败: {exc}"

    if not isinstance(data, dict) or not data.get("success"):
        msg = str(data.get("message") or data) if isinstance(data, dict) else str(data)
        return None, msg

    login_token = extract_login_token(data)
    if login_token:
        return token_credential(login_token), "登录成功并获取到新 Token"

    cookie_items = session.cookies.get_dict()
    if not cookie_items:
        set_cookies = response.headers.get("set-cookie", "") or ""
        if not set_cookies:
            set_cookies = "; ".join(
                v for k, v in response.headers.items() if k.lower() == "set-cookie"
            )
        if set_cookies:
            import http.cookies
            jar = http.cookies.SimpleCookie()
            for part in set_cookies.split(","):
                try:
                    jar.load(part.strip())
                except Exception:
                    pass
            cookie_items = {k: v.value for k, v in jar.items()}

    cookie = "; ".join(f"{key}={value}" for key, value in cookie_items.items())
    if not cookie:
        return None, "登录成功但未获取到 Token/Cookie"
    return cookie, "登录成功并获取到新 Cookie"


def login_with_password(account: Account) -> tuple[str | None, str]:
    if env_bool("NS_FORCE_BROWSER_LOGIN", False):
        print("NS_FORCE_BROWSER_LOGIN=true，跳过账号密码 API 登录，直接使用浏览器完整登录流程")
        return login_with_browser(account)

    env_captcha_token = os.getenv("NS_CAPTCHA_TOKEN", "").strip()
    if env_captcha_token:
        print("使用环境变量中的 captcha token 登录")
        credential, message = _attempt_login(account, env_captcha_token)
        if credential:
            return credential, message
        print(f"带环境 captcha token 的 API 登录未获取到可用凭据: {message}")

    endpoint = cf_solver_endpoint()
    if endpoint:
        print("尝试通过 CF_SOLVER 获取 Turnstile token...")
        token, token_message = solve_turnstile_token()
        if token:
            cookie, message = _attempt_login(account, token)
            if cookie:
                return cookie, message
            print(f"带 captcha API 登录失败: {message}")

    print("使用浏览器完整登录流程...")
    return login_with_browser(account)


def github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "nodeSeek-signin",
    }


def encrypt_for_github_secret(public_key_value: str, secret_value: str) -> str:
    import base64
    from nacl import encoding, public

    public_key = public.PublicKey(public_key_value.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("utf-8")


def github_error_detail(response) -> str:
    accepted_permissions = response.headers.get("x-accepted-github-permissions", "")
    oauth_scopes = response.headers.get("x-oauth-scopes", "")
    detail = f"HTTP {response.status_code}: {shorten_response(response.text, 500)}"
    if accepted_permissions:
        detail += f"; accepted-permissions={accepted_permissions}"
    if oauth_scopes:
        detail += f"; oauth-scopes={oauth_scopes}"
    return detail


def save_cookie_to_github_secret(cookie_value: str) -> bool:
    if not env_bool("SAVE_COOKIE_TO_GITHUB", True):
        print("SAVE_COOKIE_TO_GITHUB=false，跳过 GitHub Secret 写回")
        return True

    token = os.getenv("GH_PAT", "").strip()
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    secret_name = os.getenv("GITHUB_COOKIE_SECRET_NAME", "NS_COOKIE").strip() or "NS_COOKIE"
    if not token or not repo:
        print(f"GH_PAT 或 GITHUB_REPOSITORY 未配置，跳过 {secret_name} Secret 自动更新")
        return True

    api_base = f"https://api.github.com/repos/{repo}/actions/secrets"
    headers = github_headers(token)
    try:
        key_response = requests.get(f"{api_base}/public-key", headers=headers, timeout=30)
        if key_response.status_code != 200:
            print(f"获取 GitHub 仓库 public key 失败: {github_error_detail(key_response)}")
            return False
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
        print(f"GitHub Secret {secret_name} 已更新，下次 Actions 运行会使用新登录凭据")
        return True

    print(f"更新 GitHub Secret {secret_name} 失败: HTTP {update_response.status_code} {update_response.text}")
    return False


def mask_account_name(account_name: str) -> str:
    if not account_name:
        return "账号"
    if account_name.startswith("账号"):
        return account_name
    if len(account_name) <= 2:
        return account_name[0] + "***"
    return account_name[:2] + "***"


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
        account_name = result.account_name or f"账号{result.index}"
        lines.append(f"账号：{mask_account_name(account_name)}")

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


def send_telegram_photo(path: Path, caption: str) -> None:
    token = os.getenv("TG_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TG_USER_ID", "").strip()
    if not token or not chat_id:
        return
    if not path.is_file():
        print(f"Telegram 图片发送跳过，文件不存在: {path}")
        return

    api_host = (os.getenv("TG_API_HOST") or "https://api.telegram.org").rstrip("/")
    payload = {
        "chat_id": chat_id,
        "caption": caption,
    }
    thread_id = os.getenv("TG_THREAD_ID", "").strip()
    if thread_id:
        payload["message_thread_id"] = int(thread_id) if thread_id.isdigit() else thread_id

    mime = CurlMime()
    for key, value in payload.items():
        mime.addpart(name=key, data=str(value))
    mime.addpart(
        name="photo",
        filename=path.name,
        local_path=str(path),
        content_type="image/png",
    )

    try:
        response = requests.post(
            f"{api_host}/bot{token}/sendPhoto",
            multipart=mime,
            timeout=30,
            impersonate=impersonate_version(),
        )
        response.raise_for_status()
        print("Telegram 登录截图发送成功")
    except Exception as exc:
        print(f"Telegram 登录截图发送失败: {exc}")
    finally:
        mime.close()


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
        account_name = account.user if account else f"账号{index}"
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
                    print(f"账号 {index} {login_message}，重新签到")
                    result = sign_cookie(new_cookie, index, random_enabled)
                    print(f"账号 {index} [{result.status}]: {result.message}")
                    if result.ok:
                        cookies[index - 1] = new_cookie
                        cookies_updated = True
                    else:
                        print(f"账号 {index} 新登录凭据签到仍失败，不写回 GitHub Secret")
                else:
                    result = SignResult(index, False, "login_failed", login_message)
                    print(f"账号 {index} 自动登录失败: {login_message}")
            elif should_retry_with_login(result):
                print(f"账号 {index} 未配置对应 USER/PASS，无法自动登录刷新 Cookie")

        result.account_name = account_name
        if result.ok:
            effective_cookie = cookies[index - 1]
            result.stats = get_signin_stats(effective_cookie, 30)

        results.append(result)

    report = build_report(results)
    print("\n" + report)
    send_telegram(report)

    if cookies_updated:
        merged_cookie = join_cookies_for_secret(cookies)
        print("\n检测到登录凭据更新，准备写回 GitHub Secret NS_COOKIE")
        cookie_save_failed = not save_cookie_to_github_secret(merged_cookie)

    has_failure = any(not result.ok for result in results)
    if fail_on_error and (has_failure or cookie_save_failed):
        print("\n存在签到失败账号或登录凭据写回失败，任务标记为失败。若不希望失败退出，可设置变量 FAIL_ON_ERROR=false。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
