# -*- coding: utf-8 -*-
"""
调拨仓异常包裹监控 - 路由模块
包含：/abnormal, /api/abnormal_parcels, /api/abnormal_parcels/process
"""
import threading
from collections import OrderedDict
from flask import Blueprint, render_template, jsonify, request
from datetime import datetime, timedelta
import database
from spiders.base import get_logical_date

bp = Blueprint('abnormal', __name__)


@bp.route('/abnormal')
def abnormal_page():
    """访问异常监控页面时激活同步引擎，并立即抓取一次异常包裹"""
    from scheduler import activate_sync
    from spiders.abnormal import fetch_abnormal_parcels
    activate_sync()
    threading.Thread(target=fetch_abnormal_parcels, daemon=True).start()
    return render_template('abnormal.html')


@bp.route('/api/abnormal_parcels')
def get_abnormal_parcels():
    now = datetime.now()
    logical_today = now - timedelta(days=1) if now.hour < 8 else now
    logical_tomorrow = logical_today + timedelta(days=1)
    appointment_date = logical_tomorrow.strftime("%Y-%m-%d")

    conn = database.get_db()
    c = conn.cursor()

    c.execute('''
        SELECT * FROM abnormal_parcels 
        WHERE record_date = ? 
        ORDER BY create_time DESC
    ''', (appointment_date,))

    records = [dict(row) for row in c.fetchall()]
    conn.close()

    # 仓库ID -> 名称映射
    WH_NAME_MAP = {
        349: '惠州惠城仓', 479: '深圳龙华二仓', 501: '深圳福永仓',
        557: '深圳南山仓', 636: '深圳清溪仓', 100: '深圳平湖仓',
        189: '深圳光明仓', 546: '东莞虎门仓', 616: '东莞信立仓',
    }

    grouped = OrderedDict()
    for row in records:
        wh_id = row.get('allot_in_warehouse_id')
        wh = WH_NAME_MAP.get(wh_id, f'仓库{wh_id}') if wh_id else '调拨仓（历史数据）'
        if wh not in grouped:
            grouped[wh] = []
        grouped[wh].append(row)

    result = []
    for wh_name, rows in grouped.items():
        unprocessed = [r for r in rows if r.get('is_processed') != 1]
        diff_total = sum(r.get('qty_diff', 0) for r in unprocessed)
        result.append({
            'warehouse': wh_name,
            'rows': rows,
            'unprocessed_count': len(unprocessed),
            'diff_total': diff_total,
        })

    return jsonify(result)


@bp.route('/api/abnormal_parcels/process', methods=['POST'])
def process_abnormal_parcel():
    """标记取消包裹为已处理 / 未处理"""
    data = request.get_json(force=True)
    record_id = data.get('id')
    is_processed = data.get('is_processed', 0)
    if record_id is None:
        return jsonify({'ok': False, 'msg': '缺少 id'}), 400
    conn = database.get_db()
    c = conn.cursor()
    c.execute('UPDATE abnormal_parcels SET is_processed = ? WHERE id = ?', (is_processed, record_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})
