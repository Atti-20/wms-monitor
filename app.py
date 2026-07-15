# -*- coding: utf-8 -*-
"""
WMS 拣货监控系统 - 主入口
职责：初始化 Flask、注册路由蓝图、启动调度器
"""
import os
import sys
import subprocess
import signal
import atexit
from flask import Flask, render_template, jsonify, request, send_from_directory
from flask_cors import CORS
import database
from token_manager import TOKEN_STATUS, get_access_token

# ===== Cloudflare Tunnel 自动管理 =====
_tunnel_process = None

CLOUDFLARED_EXE = os.environ.get(
    "CLOUDFLARED_EXE",
    r"C:\Users\程旭同\AppData\Local\Microsoft\WinGet\Packages"
    r"\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"
)
CLOUDFLARED_CONFIG = os.environ.get(
    "CLOUDFLARED_CONFIG",
    r"C:\Users\程旭同\.cloudflared\config.yml"
)
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def start_tunnel():
    """启动 cloudflared tunnel 子进程"""
    global _tunnel_process
    if _tunnel_process and _tunnel_process.poll() is None:
        return  # 已经在运行

    if not os.path.isfile(CLOUDFLARED_EXE):
        print(f"[Tunnel] cloudflared 未找到: {CLOUDFLARED_EXE}，跳过隧道启动")
        return

    os.makedirs(LOG_DIR, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_out = open(os.path.join(LOG_DIR, f"cloudflared_{ts}.log"), "w")
    log_err = open(os.path.join(LOG_DIR, f"cloudflared_err_{ts}.log"), "w")

    cmd = [CLOUDFLARED_EXE, "tunnel", "--config", CLOUDFLARED_CONFIG, "run"]
    _tunnel_process = subprocess.Popen(
        cmd, stdout=log_out, stderr=log_err,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    print(f"[Tunnel] cloudflared 已启动 (PID={_tunnel_process.pid})")


def stop_tunnel():
    """停止 cloudflared tunnel 子进程"""
    global _tunnel_process
    if _tunnel_process is None:
        return
    if _tunnel_process.poll() is not None:
        _tunnel_process = None
        return
    print(f"[Tunnel] 正在停止 cloudflared (PID={_tunnel_process.pid})...")
    try:
        if sys.platform == "win32":
            _tunnel_process.terminate()
        else:
            _tunnel_process.send_signal(signal.SIGTERM)
        _tunnel_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _tunnel_process.kill()
    except Exception as e:
        print(f"[Tunnel] 停止异常: {e}")
    _tunnel_process = None
    print("[Tunnel] cloudflared 已停止")


# 注册退出钩子，确保 Flask 退出时关闭 tunnel
atexit.register(stop_tunnel)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})
database.init_db()

# 初始化数据引擎（共享 session）
print("初始化数据引擎...")
from spiders.base import get_shared_session
get_shared_session()

# 启动定时调度器（导入即启动）
import scheduler

# ===== 注册路由蓝图 =====
from routes.dashboard import bp as dashboard_bp
from routes.abnormal import bp as abnormal_bp
from routes.cancel import bp as cancel_bp
from routes.blame import bp as blame_bp
from routes.attendance import bp as attendance_bp
from routes.permission import bp as permission_bp

app.register_blueprint(dashboard_bp)
app.register_blueprint(abnormal_bp)
app.register_blueprint(cancel_bp)
app.register_blueprint(blame_bp)
app.register_blueprint(attendance_bp)
app.register_blueprint(permission_bp)


# ===== 微信域名验证（根路径 .txt 文件） =====
@app.route('/<path:filename>')
def wechat_verify(filename):
    """供微信等平台在根路径下访问验证文件"""
    if filename.endswith('.txt'):
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
        return send_from_directory(static_dir, filename)
    return ('Not Found', 404)


# ===== 公共路由（门户 + Token 管理） =====
@app.route('/')
def portal():
    return render_template('portal.html')


@app.route('/api/login', methods=['POST'])

def api_login():
    """简单的访问密码验证"""
    data = request.get_json(force=True)
    password = data.get('password', '')
    if password == '888888':
        return jsonify({"ok": True, "msg": "验证成功"})
    return jsonify({"ok": False, "msg": "密码错误"})


@app.route('/api/token_status')
def get_token_status():
    """查询 SSO Token 当前状态"""
    return jsonify(TOKEN_STATUS)


@app.route('/api/cookie_status')
def get_cookie_status():
    """返回鉴权状态（Token 或 Cookie 模式）"""
    from spiders.base import get_user_cookie
    status = dict(TOKEN_STATUS)
    if get_user_cookie():
        status["has_cookie"] = True
        if not status["ok"]:
            status["ok"] = True
            status["method"] = "cookie"
    else:
        status["has_cookie"] = False
    return jsonify(status)


@app.route('/api/refresh_token', methods=['POST'])
def refresh_token():
    """手动触发 Token 刷新"""
    try:
        token = get_access_token(force_refresh=True)
        if token:
            return jsonify({"ok": True, "msg": "Token 刷新成功"})
        else:
            return jsonify({"ok": False, "msg": "Token 刷新失败，请确认本机 MOA 已登录"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"刷新异常: {str(e)}"})


@app.route('/api/update_cookie', methods=['POST'])
def update_cookie():
    """接收用户手动输入的 Cookie（备选鉴权方案）"""
    from spiders.base import save_user_cookie, get_user_cookie
    try:
        data = request.get_json(force=True)
        cookie_str = data.get('cookie', '').strip()

        if cookie_str:
            # 用户提供了 Cookie，保存并切换为 Cookie 模式
            save_user_cookie(cookie_str)
            TOKEN_STATUS["ok"] = True
            TOKEN_STATUS["method"] = "cookie"
            TOKEN_STATUS["last_refresh"] = __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            TOKEN_STATUS["last_error"] = None
            return jsonify({"ok": True, "msg": "Cookie 已保存，系统将使用 Cookie 直连模式"})
        else:
            # 未提供 Cookie，尝试刷新 Token
            token = get_access_token(force_refresh=True)
            if token:
                return jsonify({"ok": True, "msg": "SSO Token 刷新成功"})
            elif get_user_cookie():
                return jsonify({"ok": True, "msg": "当前使用 Cookie 直连模式（仍有效）"})
            else:
                return jsonify({"ok": False, "msg": "Token 刷新失败，请输入 Cookie 或确认 MOA 已登录"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"操作异常: {str(e)}"})


@app.route('/api/deactivate', methods=['POST'])
def api_deactivate():
    """用户关闭页面时调用，立即停止后台数据抓取"""
    from scheduler import deactivate_sync
    deactivate_sync()
    return jsonify({"ok": True, "msg": "数据抓取已停止"})


@app.route('/api/warehouse_list')
def warehouse_list():
    """获取支持的仓库列表（仓库选择由前端 localStorage 管理，服务端不保存状态）"""
    from spiders.base import WAREHOUSES, DEFAULT_WAREHOUSE_ID
    warehouses = [
        {"id": wh_id, "name": info["name"], "short_name": info["short_name"]}
        for wh_id, info in WAREHOUSES.items()
    ]
    return jsonify({"ok": True, "warehouses": warehouses, "default": DEFAULT_WAREHOUSE_ID})


if __name__ == '__main__':
    # 启动 Cloudflare Tunnel（仅主进程，debug 模式下 reloader 会 fork 子进程）
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
        start_tunnel()

    try:
        app.run(host='0.0.0.0', port=5000, debug=True)
    except KeyboardInterrupt:
        pass
    finally:
        stop_tunnel()
