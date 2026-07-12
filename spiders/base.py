# -*- coding: utf-8 -*-
"""
公共请求工具与基础配置
"""
import time
import os
import json
import requests
from urllib.parse import urlencode
from datetime import datetime, timedelta
from token_manager import get_access_token, invalidate_token, TOKEN_STATUS, PROXY_URL

# ===================== 配置项 =====================
REQUEST_TIMEOUT = 15
NEW_STAFF_DAYS = 7
STAGNANT_MINUTES = 10
WAREHOUSE_ID = "428"
BASE_URL = "https://klwms.meituan.com"


# ===================== 工具函数 =====================
def safe_get_row_value(row, key, default=None):
    """安全获取 sqlite3.Row 对象的值"""
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def map_wave_name_to_id(wave_name):
    """将波次名称映射为 ID"""
    if not wave_name:
        return None
    if "上午达" in wave_name:
        return 1
    if "下午达" in wave_name:
        return 2
    if "凌晨达" in wave_name:
        return 5
    return None


def parse_ms_timestamp(ts):
    """转换毫秒级时间戳为 datetime 对象"""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts) / 1000.0)
    except (ValueError, TypeError):
        return None


def get_logical_date():
    """获取逻辑日期（8点前算前一天）"""
    now = datetime.now()
    if now.hour < 8:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        return now.strftime("%Y-%m-%d")


# ===================== Cookie 直连模式 =====================
_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".user_cookie.json")
_use_cookie_mode = False  # 是否已降级为 Cookie 直连


def get_user_cookie():
    """读取用户手动输入的 Cookie"""
    try:
        path = os.path.normpath(_COOKIE_FILE)
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        cookie_str = data.get("cookie", "")
        if not cookie_str:
            return None
        # 检查是否过期（保存后 24 小时内有效）
        save_time = data.get("save_time", 0)
        if time.time() - save_time > 86400:
            return None
        return cookie_str
    except Exception:
        return None


def save_user_cookie(cookie_str):
    """保存用户手动输入的 Cookie"""
    path = os.path.normpath(_COOKIE_FILE)
    data = {"cookie": cookie_str, "save_time": time.time()}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    print("[OK] 用户 Cookie 已保存")


def _request_via_cookie(session, original_url, max_retries=2):
    """通过 Cookie 直连 klwms 接口（备选方案）"""
    cookie_str = get_user_cookie()
    if not cookie_str:
        return None

    for attempt in range(max_retries):
        try:
            headers = {
                "Cookie": cookie_str,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0",
            }
            response = session.get(original_url, headers=headers, timeout=REQUEST_TIMEOUT)

            if response.status_code == 401 or response.status_code == 403:
                print("[ERROR] Cookie 直连返回 鉴权失败, Cookie 可能已过期")
                TOKEN_STATUS["ok"] = False
                TOKEN_STATUS["last_error"] = "Cookie 已过期，请重新输入"
                return None

            if response.status_code == 429:
                time.sleep(1)
                continue

            response.raise_for_status()
            res_data = response.json()
            TOKEN_STATUS["ok"] = True
            TOKEN_STATUS["method"] = "cookie"
            return res_data

        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Cookie 直连失败 (第{attempt + 1}次): {e}")
            if attempt == max_retries - 1:
                return None
        except ValueError:
            return None
    return None


# ===================== 请求工具（NoCode Proxy + Cookie 降级） =====================
def request_with_retry(session, api_path, params=None, max_retries=2):
    """
    请求接口，优先 NoCode Proxy，失败时自动降级为 Cookie 直连。
    """
    global _use_cookie_mode

    if api_path.startswith("http"):
        original_url = api_path
    else:
        original_url = f"{BASE_URL}{api_path}"

    if params:
        query_string = urlencode(params, doseq=True)
        original_url = f"{original_url}?{query_string}"

    # 如果已知 Proxy 不可用且有 Cookie，直接走 Cookie 模式
    if _use_cookie_mode and get_user_cookie():
        result = _request_via_cookie(session, original_url, max_retries)
        if result is not None:
            return result
        # Cookie 也失败了，尝试回退到 Proxy
        _use_cookie_mode = False

    # 优先走 NoCode Proxy
    for attempt in range(max_retries):
        try:
            token = get_access_token()
            if not token:
                print("[ERROR] 无法获取有效 Token, 尝试 Cookie 降级...")
                break  # 跳出循环去走 Cookie 降级

            headers = {
                "Origin-Url": original_url,
                "access-token": token,
            }

            response = session.get(
                PROXY_URL,
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 401:
                print("[ERROR] HTTP 401 鉴权失败, Token 已失效, 正在尝试刷新...")
                invalidate_token()
                token = get_access_token(force_refresh=True)
                if not token:
                    print("[ERROR] Token 刷新失败")
                    break
                headers["access-token"] = token
                response = session.get(
                    PROXY_URL,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT
                )
                if response.status_code == 401:
                    print("[ERROR] Token 刷新后仍然 401")
                    break

            if response.status_code == 429:
                print("[WARN] 请求被限流 (429), 等待 1s 后重试...")
                time.sleep(1)
                continue

            response.raise_for_status()
            res_data = response.json()

            if res_data.get('code') == 401:
                print("[ERROR] 接口返回 401, Token 已失效, 正在刷新...")
                invalidate_token()
                if attempt < max_retries - 1:
                    continue
                break

            if res_data.get('code') == 50001:
                print("[ERROR] Proxy 返回 50001: 域名不在白名单")
                return None

            # Proxy 成功，重置 Cookie 模式标记
            _use_cookie_mode = False
            return res_data

        except requests.exceptions.ConnectionError as e:
            print(f"[ERROR] Proxy 连接失败 (第{attempt + 1}次): {e}")
            if attempt == max_retries - 1:
                print("[INFO] Proxy 不可用，尝试 Cookie 降级...")
                _use_cookie_mode = True
                break
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] 请求失败 (第{attempt + 1}次): {e}")
            if attempt == max_retries - 1:
                print("[ERROR] 多次重试后请求仍失败")
                break
        except ValueError as e:
            print(f"[ERROR] JSON解析失败: {e}")
            return None

    # Proxy 失败，尝试 Cookie 直连降级
    cookie_result = _request_via_cookie(session, original_url, max_retries)
    if cookie_result is not None:
        _use_cookie_mode = True
        print("[OK] 已降级为 Cookie 直连模式")
        return cookie_result

    print("[ERROR] Proxy 和 Cookie 直连均失败")
    TOKEN_STATUS["ok"] = False
    TOKEN_STATUS["last_error"] = "Proxy 不可用且无有效 Cookie"
    return None


# ===================== 共享 Session =====================
_shared_session = None


def get_shared_session():
    """获取全局共享的 requests.Session"""
    global _shared_session
    if _shared_session is None:
        _shared_session = requests.Session()
        _shared_session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0",
            "Content-Type": "application/json"
        })
        token = get_access_token()
        if token:
            print("[OK] SSO Token 获取成功, 系统就绪")
        elif get_user_cookie():
            print("[OK] 检测到有效 Cookie, 系统将使用 Cookie 直连模式")
        else:
            print("[WARN] SSO Token 获取失败且无 Cookie, 请在页面中手动输入 Cookie")
    return _shared_session
