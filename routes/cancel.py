# -*- coding: utf-8 -*-
"""
取消件/翻包件 - 路由模块
包含：/cancel_detail, /api/cancel_detail, /api/fetch_cancel_data,
      /api/download_excel, /api/cancel_parcel_summary
"""
import os
from flask import Blueprint, render_template, jsonify, request, send_file
import database
import cancel_parcel
from token_manager import get_access_token
from spiders.base import get_logical_date, WAREHOUSES, DEFAULT_WAREHOUSE_ID

bp = Blueprint('cancel', __name__)


def _req_warehouse_id():
    """从请求参数中获取仓库ID（客户端级别，不影响全局状态）"""
    wh_id = request.args.get('warehouseId', '').strip()
    if wh_id and wh_id in WAREHOUSES:
        return wh_id
    return DEFAULT_WAREHOUSE_ID


@bp.route('/cancel_detail')
def cancel_detail_page():
    return render_template('cancel_detail.html')


@bp.route('/api/cancel_detail')
def cancel_detail_api():
    wh_id = _req_warehouse_id()
    date_str = request.args.get('date', get_logical_date())
    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM cancel_parcel_log
        WHERE record_date = ?
        ORDER BY create_time DESC
    ''', (date_str,))
    records = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(records)


@bp.route('/api/fetch_cancel_data')
def fetch_cancel_data():
    """抓取清溪仓非翻包取消件明细"""
    token = get_access_token()
    if not token:
        return jsonify({'status': 'error', 'msg': 'SSO Token 未获取，请确认本机 MOA 已登录'}), 500
    try:
        excel_path, table_data = cancel_parcel.generate_excel_for_web(token)
        filename = os.path.basename(excel_path)
        return jsonify({'status': 'success', 'data': table_data, 'filename': filename})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500


@bp.route('/api/download_excel')
def download_excel():
    """下载已生成的 Excel 文件"""
    filename = request.args.get('file', '')
    if not filename or '..' in filename:
        return jsonify({'msg': '非法文件名'}), 400
    # Excel 文件生成在项目根目录（wms_monitor/）
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    filepath = os.path.join(project_root, filename)
    if not os.path.exists(filepath):
        return jsonify({'msg': '文件不存在，请重新抓取'}), 404
    return send_file(filepath, as_attachment=True, download_name=filename)


@bp.route('/api/cancel_parcel_summary')
def cancel_parcel_summary():
    wh_id = _req_warehouse_id()
    date_str = request.args.get('date', get_logical_date())
    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('''
        SELECT record_date, COUNT(*) as total_count,
               SUM(CASE WHEN parcel_type = '取消件' THEN 1 ELSE 0 END) as cancel_count,
               SUM(CASE WHEN parcel_type = '翻包件' THEN 1 ELSE 0 END) as flip_count
        FROM cancel_parcel_log
        WHERE record_date = ?
        GROUP BY record_date
    ''', (date_str,))
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify(dict(row))
    return jsonify({'record_date': date_str, 'total_count': 0, 'cancel_count': 0, 'flip_count': 0})
