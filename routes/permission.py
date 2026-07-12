# -*- coding: utf-8 -*-
"""
拣货人员权限检查 - 路由模块
包含：/permission 页面, /api/permission/check, /api/permission/last_result
"""
import threading
from flask import Blueprint, render_template, jsonify, request

bp = Blueprint('permission', __name__)

# 防止并发执行权限检查
_check_lock = threading.Lock()
_check_running = False

AUTH_PASSWORD = '888888'


def _check_auth():
    """从请求头 X-Auth-Token 验证密码，返回 True 表示通过"""
    token = request.headers.get('X-Auth-Token', '')
    return token == AUTH_PASSWORD


@bp.route('/permission')
def permission_page():
    """权限检查页面"""
    return render_template('permission.html')


@bp.route('/api/permission/check', methods=['POST'])
def api_check_permissions():
    """手动触发权限检查（异步执行），需要认证"""
    if not _check_auth():
        return jsonify({"ok": False, "msg": "未授权，请先登录"}), 403

    global _check_running
    if _check_running:
        return jsonify({"ok": False, "msg": "权限检查正在执行中，请稍后再试"})

    if not _check_lock.acquire(blocking=False):
        return jsonify({"ok": False, "msg": "权限检查正在执行中，请稍后再试"})

    def _run():
        global _check_running
        _check_running = True
        try:
            from spiders.permission import check_and_fix_permissions
            check_and_fix_permissions()
        except Exception as e:
            print(f"[权限检查] 异步执行异常: {e}")
        finally:
            _check_running = False
            _check_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": "权限检查已启动，请稍等片刻后刷新查看结果"})


@bp.route('/api/permission/status')
def api_check_status():
    """查询权限检查是否正在运行"""
    return jsonify({"running": _check_running})


@bp.route('/api/permission/last_result')
def api_last_result():
    """获取上次权限检查结果"""
    from spiders.permission import get_last_check_result
    result = get_last_check_result()
    if result:
        return jsonify({"ok": True, "data": result})
    else:
        return jsonify({"ok": False, "msg": "暂无检查记录"})
