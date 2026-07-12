# -*- coding: utf-8 -*-
"""
WMS 监控系统 - SSO Token 管理模块
通过 CatDesk CLI (CatDesk.exe + catpaw-cli.js) 获取 baobab-a audience 的 access_token，
并通过 NoCode Proxy 方式请求 klwms 接口。

鉴权链路：
  方式1(优先): CatDesk.exe auth exchange --target-client-id "baobab-a" -> accessToken
  方式2(回退): npx mtsso-moa-local-exchange --audience "baobab-a" -> access_token
  方式3(兜底): 读取本地缓存文件（上次成功获取的 token 如果仍在有效期内）
  -> NoCode Proxy (Origin-Url + access-token header) -> klwms.meituan.com

依赖条件：
  - CatDesk 桌面端在后台运行且已登录（开机自启，通常一直在）
  - 即使 CatDesk 短暂不可用，本地缓存的 token 在有效期内仍可继续使用
"""

import subprocess
import json
import time
import os
import threading
from datetime import datetime
from pathlib import Path

# ===================== 配置 =====================
AUDIENCE = "baobab-a"
PROXY_URL = "https://nocode.sankuai.com/proxy/request"
TOKEN_CACHE_SECONDS = 3400  # Token 缓存约 57 分钟（catdesk exchange 返回 expiresIn=3600）

# CatDesk 直接调用路径（绕过 .cmd 文件的中文路径编码问题）
_LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA", "")
CATDESK_EXE = os.path.join(_LOCAL_APP_DATA, "CatDesk", "CatDesk.exe")
CATDESK_CLI_JS = os.path.join(_LOCAL_APP_DATA, "CatDesk", "resources", "cli", "catpaw-cli.js")

# Token 本地持久化文件（确保 CatDesk 短暂不可用时仍能工作）
_TOKEN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token_cache.json")

# ===================== Token 全局状态 =====================
TOKEN_STATUS = {
    "ok": False,
    "last_refresh": None,
    "last_error": None,
    "token_expires_hint": None,
    "method": None,  # "catdesk" / "mtsso" / "file_cache"
}

_token_lock = threading.Lock()
_cached_token = None
_cached_token_time = 0


# ===================== 本地文件缓存 =====================
def _save_token_to_file(token, acquire_time):
    """将 token 持久化到本地文件，供下次启动时读取"""
    try:
        data = {
            "token": token,
            "acquire_time": acquire_time,
            "audience": AUDIENCE,
        }
        with open(_TOKEN_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    except Exception:
        pass  # 写入失败不影响主流程


def _load_token_from_file():
    """
    从本地文件读取缓存的 token（兜底方案）。
    只有在 token 仍在有效期内才返回。
    """
    try:
        if not os.path.exists(_TOKEN_CACHE_FILE):
            return None
        with open(_TOKEN_CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)

        token = data.get("token")
        acquire_time = data.get("acquire_time", 0)

        if not token:
            return None

        # 检查是否在有效期内（使用完整的 3600 秒，因为这是最后手段）
        elapsed = time.time() - acquire_time
        if elapsed > 3600:
            return None

        TOKEN_STATUS["method"] = "file_cache"
        remaining = int(3600 - elapsed)
        print(f"[INFO] 从本地缓存读取 Token (剩余有效期约 {remaining}s)")
        return token

    except Exception:
        return None


# ===================== 取票方式 =====================
def _run_catdesk_exchange():
    """
    方式1(优先): 通过 CatDesk.exe + catpaw-cli.js 直接调用 auth exchange 获取 token。
    绕过 catdesk.cmd 的中文路径编码问题。
    依赖 CatDesk 桌面端正在运行且已登录。
    """
    if not os.path.exists(CATDESK_EXE) or not os.path.exists(CATDESK_CLI_JS):
        print("[WARN] CatDesk.exe 或 catpaw-cli.js 不存在, 跳过此方式")
        return None

    try:
        env = os.environ.copy()
        env["ELECTRON_RUN_AS_NODE"] = "1"

        result = subprocess.run(
            [CATDESK_EXE, CATDESK_CLI_JS, "auth", "exchange", "--target-client-id", AUDIENCE],
            capture_output=True,
            text=True,
            timeout=15,
            encoding='utf-8',
            errors='replace',
            env=env
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "未知错误"
            print(f"[WARN] catdesk auth exchange 失败 (exit={result.returncode}): {error_msg[:150]}")
            return None

        stdout = result.stdout.strip()
        if not stdout:
            return None

        data = json.loads(stdout)
        access_token = data.get("accessToken")
        if access_token:
            TOKEN_STATUS["method"] = "catdesk"
            return access_token
        return None

    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as e:
        print(f"[WARN] catdesk auth exchange 异常: {e}")
        return None


def _run_mtsso_command():
    """
    方式2(回退): 通过 npx mtsso-moa-local-exchange 获取 token。
    依赖本机 MOA/CatDesk 登录态 + AGENT_SSO_CLIENT_ID 环境变量。
    """
    try:
        result = subprocess.run(
            ["npx", "mtsso-moa-local-exchange", "--audience", AUDIENCE, "--auto_update", "false"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding='utf-8',
            errors='replace',
            shell=True
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip() if result.stderr else "未知错误"
            print(f"[ERROR] mtsso-moa-local-exchange 失败 (exit={result.returncode}): {error_msg[:200]}")
            return None

        stdout = result.stdout.strip()
        if not stdout:
            print("[ERROR] mtsso-moa-local-exchange 返回为空")
            return None

        data = json.loads(stdout)
        access_token = data.get("access_token")

        if not access_token:
            print(f"[ERROR] 返回 JSON 中无 access_token 字段: {list(data.keys())}")
            return None

        TOKEN_STATUS["method"] = "mtsso"
        return access_token

    except subprocess.TimeoutExpired:
        print("[ERROR] mtsso-moa-local-exchange 超时(30s)")
        return None
    except json.JSONDecodeError as e:
        print(f"[ERROR] mtsso-moa-local-exchange 返回非 JSON: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] mtsso-moa-local-exchange 异常: {e}")
        return None


def _acquire_token():
    """
    尝试获取 token，按优先级：
    1. CatDesk auth exchange (只要 CatDesk 在后台运行)
    2. npx mtsso-moa-local-exchange (需要 client_id/secret 环境变量 + 登录态)
    3. 本地文件缓存 (上次成功获取的 token 如果仍在有效期内)
    """
    # 方式1: CatDesk CLI (优先，最可靠)
    token = _run_catdesk_exchange()
    if token:
        return token

    # 方式2: npx mtsso (需要额外凭据)
    token = _run_mtsso_command()
    if token:
        return token

    # 方式3: 本地文件缓存 (兜底)
    token = _load_token_from_file()
    if token:
        return token

    return None


# ===================== 对外接口 =====================
def get_access_token(force_refresh=False):
    """
    获取有效的 access_token（带内存缓存 + 文件持久化）。
    - 内存缓存有效期内直接返回
    - 过期或 force_refresh 时重新获取
    - 成功获取后会持久化到文件，下次启动可直接使用
    返回 token 字符串，失败返回 None。
    """
    global _cached_token, _cached_token_time

    with _token_lock:
        now = time.time()

        # 如果不强制刷新 且 内存缓存未过期，直接返回
        if not force_refresh and _cached_token and (now - _cached_token_time) < TOKEN_CACHE_SECONDS:
            return _cached_token

        # 需要重新获取
        print(f"[INFO] 正在获取 SSO Token (audience: {AUDIENCE})...")
        token = _acquire_token()

        if token:
            _cached_token = token
            _cached_token_time = now
            TOKEN_STATUS["ok"] = True
            TOKEN_STATUS["last_refresh"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            TOKEN_STATUS["last_error"] = None
            TOKEN_STATUS["token_expires_hint"] = datetime.fromtimestamp(
                now + TOKEN_CACHE_SECONDS
            ).strftime("%H:%M:%S")
            print(f"[OK] Token 获取成功 (via {TOKEN_STATUS['method']}), 缓存至 {TOKEN_STATUS['token_expires_hint']}")

            # 持久化到文件（非 file_cache 来源时才写入，避免循环）
            if TOKEN_STATUS["method"] != "file_cache":
                _save_token_to_file(token, now)

            return token
        else:
            TOKEN_STATUS["ok"] = False
            TOKEN_STATUS["last_error"] = "所有取票方式均失败 (CatDesk未运行?)"
            # 获取失败，如果有旧的内存缓存 token 还没过期太久，尝试继续用
            if _cached_token and (now - _cached_token_time) < TOKEN_CACHE_SECONDS * 1.5:
                print("[WARN] Token 刷新失败, 暂时使用旧 Token")
                return _cached_token
            _cached_token = None
            _cached_token_time = 0
            return None


def invalidate_token():
    """
    标记当前 Token 无效（例如收到 401 响应后调用），下次请求时会强制刷新。
    """
    global _cached_token, _cached_token_time
    with _token_lock:
        _cached_token = None
        _cached_token_time = 0
        TOKEN_STATUS["ok"] = False
        TOKEN_STATUS["last_error"] = "Token 已被标记为无效(收到 401)"
    # 同时清除文件缓存
    try:
        if os.path.exists(_TOKEN_CACHE_FILE):
            os.remove(_TOKEN_CACHE_FILE)
    except Exception:
        pass
    print("[INFO] Token 已失效, 下次请求将重新获取")


def get_proxy_headers(token=None):
    """构建 NoCode Proxy 请求所需的 headers。"""
    if token is None:
        token = get_access_token()
    if not token:
        return None
    return {
        "access-token": token,
        "Content-Type": "application/json",
    }


def build_proxy_request(original_url):
    """
    将原始 klwms URL 转换为 NoCode Proxy 请求格式。

    Args:
        original_url: 完整的原始 URL（含参数），如 https://klwms.meituan.com/xxx?a=1&b=2

    Returns:
        dict: {"url": proxy_url, "headers": {"Origin-Url": original_url, "access-token": token}}
        或 None（token 获取失败时）
    """
    token = get_access_token()
    if not token:
        return None

    return {
        "url": PROXY_URL,
        "headers": {
            "Origin-Url": original_url,
            "access-token": token,
        }
    }
