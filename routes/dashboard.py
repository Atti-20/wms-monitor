# -*- coding: utf-8 -*-
"""
拣货人员监控 - 路由模块
包含：/dashboard, /api/dashboard, /api/export_worker_data,
      /api/picking_stagnant, /api/exclude_worker 等
"""
import csv
import io
from flask import Blueprint, render_template, jsonify, request, Response
from datetime import datetime, timedelta
import database
from spiders.base import get_logical_date, get_warehouse_id, get_warehouse_name, WAREHOUSES, DEFAULT_WAREHOUSE_ID

bp = Blueprint('dashboard', __name__)


def _req_warehouse_id():
    """从请求参数中获取仓库ID（客户端级别，不影响全局状态）"""
    wh_id = request.args.get('warehouseId', '').strip()
    if wh_id and wh_id in WAREHOUSES:
        return wh_id
    return DEFAULT_WAREHOUSE_ID


def _short_team(name):
    """班组简称：去前缀去后缀，保留核心区分词"""
    if not name:
        return ''
    s = name
    # 提取(B套)标记
    suffix = ''
    if '(B套)' in s:
        suffix = 'B'
        s = s.replace('(B套)', '')
    # 去掉常见前缀
    if s.startswith("自营标品"):
        s = s[4:]
    elif s.startswith("输送线"):
        # 输送线系列：去掉前缀，保留"中组""乙组"，不加B套后缀
        s = s[3:]
        return s or name
    # 去掉末尾的"组"
    if s.endswith('组'):
        s = s[:-1]
    # "蔬菜投线" 简化为 "蔬菜"
    if s == "蔬菜投线":
        s = "蔬菜"
    return (s + suffix) or name


def _wage_type_label(job_type):
    """根据岗位描述(job_type)判断计时/计件标签。
    实际岗位描述文案多样（如 "FDC-生产辅助员(计时)"、"自动化拣货员（计件）" 等），
    统一按是否包含"计时"/"计件"关键字判断，不做固定文案匹配，未命中则返回空字符串。
    """
    if not job_type:
        return ''
    if '计时' in job_type:
        return '计时'
    if '计件' in job_type:
        return '计件'
    return ''


def _get_activate_sync():
    """延迟导入 activate_sync，避免循环依赖"""
    from scheduler import activate_sync
    return activate_sync


@bp.route('/dashboard')
def dashboard_page():
    _get_activate_sync()()
    return render_template('index.html')


@bp.route('/api/dashboard')
def dashboard_api():
    _get_activate_sync()()
    wh_id = _req_warehouse_id()
    conn = database.get_db(wh_id)
    c = conn.cursor()

    # 1. 查询人员实时明细
    c.execute('''
        SELECT w.name, w.team_name, w.job_type, w.is_new_staff, w.is_temp_worker, w.total_count, w.feeding_area_count, w.seasoning_area_count, 
               w.efficiency, w.idle_minutes, w.is_stagnant, w.max_idle_minutes, w.stagnant_10min_count, w.update_time,
               w.clockin_time, w.last_modify_time,
               COALESCE(w.seasoning_bill_count, 0) AS seasoning_bill_count,
               COALESCE(w.feeding_bill_count, 0) AS feeding_bill_count,
               COALESCE(ic.is_excluded, 0) AS is_excluded
        FROM worker_realtime w
        LEFT JOIN worker_info_cache ic ON w.name = ic.name
        ORDER BY w.is_stagnant DESC, w.efficiency DESC
    ''')
    workers = [dict(row) for row in c.fetchall()]

    raw_times = [w['update_time'] for w in workers if w.get('update_time')]
    data_update_time = max(raw_times) if raw_times else None
    if data_update_time:
        data_update_time = data_update_time[11:16]

    # 在线人数：非停滞的工人
    online_workers_count = sum(1 for w in workers if not w.get('is_stagnant'))
    # 参与人数：除了非拣货人员，其他都算入
    participating_count = sum(1 for w in workers if not w.get('is_excluded'))

    # 2. 查询各仓单量
    today = get_logical_date()
    c.execute('''
        SELECT name, warehouse_name, SUM(bill_count) as bill_count
        FROM worker_warehouse_wave_bill_count
        WHERE work_date = ?
        GROUP BY name, warehouse_name
    ''', (today,))
    bill_rows = c.fetchall()

    worker_bills_map = {}
    worker_tasks_map = {}
    for row in bill_rows:
        name = row['name']
        if name not in worker_bills_map:
            worker_bills_map[name] = []
        worker_bills_map[name].append(f"{row['warehouse_name']}:{row['bill_count']}")
        worker_tasks_map[name] = worker_tasks_map.get(name, 0) + row['bill_count']

    # 批量查询今日下班打卡时间
    c.execute('''
        SELECT name, clockout_time FROM daily_attendance_cache
        WHERE attence_date = ? AND clockout_time IS NOT NULL
    ''', (today,))
    raw_clockout = {row['name']: row['clockout_time'] for row in c.fetchall()}

    def is_valid_clockout(clockout_str):
        try:
            dt = datetime.strptime(clockout_str, "%Y-%m-%d %H:%M:%S")
            h, m = dt.hour, dt.minute
            if h >= 22 or (h == 21 and m >= 30) or h <= 1:
                return True
            return False
        except Exception:
            return False

    clockout_map = {}
    for name, ct in raw_clockout.items():
        if is_valid_clockout(ct):
            clockout_map[name] = ct

    # 查询当天每人停滞次数和总时长
    logical_today_dt = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
    stagnant_start = logical_today_dt.strftime("%Y-%m-%d") + " 06:00:00"
    stagnant_end = (logical_today_dt + timedelta(days=1)).strftime("%Y-%m-%d") + " 08:00:00"
    c.execute('''
        SELECT handler_name,
               COUNT(*) as stagnant_count,
               SUM(CASE WHEN stagnant_duration_minutes > 0 THEN stagnant_duration_minutes ELSE stagnant_minutes END) as total_duration
        FROM picking_stagnant_log
        WHERE record_time >= ? AND record_time < ?
          AND (CASE WHEN stagnant_duration_minutes > 0 THEN stagnant_duration_minutes ELSE stagnant_minutes END) >= 10
        GROUP BY handler_name
    ''', (stagnant_start, stagnant_end))
    worker_stagnant_map = {}
    for row in c.fetchall():
        worker_stagnant_map[row["handler_name"]] = {
            "count": row["stagnant_count"],
            "duration": round(row["total_duration"] or 0, 1),
        }

    # 读取上次权限检查结果，构建 name → has_gxjhsm 映射
    perm_map = {}  # name → True/False/None(未检查)
    c.execute("SELECT value FROM kv_store WHERE key = 'last_permission_check'")
    perm_row = c.fetchone()
    if perm_row:
        import json as _json
        try:
            perm_data = _json.loads(perm_row['value'])
            for d in perm_data.get('details', []):
                if d.get('status') == 'ok' or d.get('status') == 'fixed':
                    perm_map[d['name']] = True
                elif d.get('status') in ('fix_failed', 'error'):
                    perm_map[d['name']] = False
                elif d.get('status') == 'skipped':
                    perm_map[d['name']] = None
                else:
                    perm_map[d['name']] = None
        except Exception:
            pass

    teams_set = set()
    for worker in workers:
        worker['bill_text'] = ' '.join(worker_bills_map.get(worker['name'], []))
        worker['total_tasks'] = worker_tasks_map.get(worker['name'], 0)
        worker['clockout_time'] = clockout_map.get(worker['name'])
        st = worker_stagnant_map.get(worker['name'], {})
        worker['stagnant_count'] = st.get('count', 0)
        worker['stagnant_total_minutes'] = st.get('duration', 0)
        if worker['clockout_time'] or worker.get('is_excluded'):
            worker['is_stagnant_alert'] = False
        else:
            worker['is_stagnant_alert'] = worker['is_stagnant'] or (float(worker['idle_minutes'] or 0) >= 10)
        # 注入权限状态: True=已有, False=缺失, None=未检查/不在名单
        worker['has_gxjhsm'] = perm_map.get(worker['name'])
        if worker['team_name']:
            teams_set.add(worker['team_name'])

    # 3. 查仓储宏观数据
    c.execute('''
        SELECT warehouse_name, fulfillment_wave, volume, picked_volume, need_allot_total_count,
               seasoning_bill_count, feeding_bill_count
        FROM global_warehouse_realtime_wave
    ''')
    wave_rows = c.fetchall()

    warehouse_wave_data = {}
    warehouse_total_vols = {}
    wave_totals = {
        1: {'picked': 0, 'need_allot': 0, 'total_volume': 0, 'seasoning_bills': 0, 'feeding_bills': 0},
        2: {'picked': 0, 'need_allot': 0, 'total_volume': 0, 'seasoning_bills': 0, 'feeding_bills': 0},
        5: {'picked': 0, 'need_allot': 0, 'total_volume': 0, 'seasoning_bills': 0, 'feeding_bills': 0}
    }
    totalPicked, totalNeedAllot, totalUnpicked, totalSeasoningBills, totalFeedingBills = 0, 0, 0, 0, 0
    warehouse_area_unpicked = {}

    for row in wave_rows:
        wh = row['warehouse_name']
        wave_id = row['fulfillment_wave']
        vol = row['volume'] or 0
        picked = row['picked_volume'] or 0
        need = row['need_allot_total_count'] or 0
        seasoning = row['seasoning_bill_count'] or 0
        feeding = row['feeding_bill_count'] or 0

        if wh not in warehouse_wave_data:
            warehouse_wave_data[wh] = {}
        warehouse_wave_data[wh][wave_id] = {'volume': vol, 'picked': picked, 'need_allot': need}
        warehouse_total_vols[wh] = warehouse_total_vols.get(wh, 0) + vol

        totalPicked += picked
        totalNeedAllot += need
        totalUnpicked += max(0, vol - picked)
        totalSeasoningBills += seasoning
        totalFeedingBills += feeding

        if wave_id in wave_totals:
            wave_totals[wave_id]['picked'] += picked
            wave_totals[wave_id]['need_allot'] += need
            wave_totals[wave_id]['total_volume'] += vol
            wave_totals[wave_id]['seasoning_bills'] += seasoning
            wave_totals[wave_id]['feeding_bills'] += feeding

        if wh not in warehouse_area_unpicked:
            warehouse_area_unpicked[wh] = {'seasoning': 0, 'feeding': 0}
        warehouse_area_unpicked[wh]['seasoning'] += seasoning
        warehouse_area_unpicked[wh]['feeding'] += feeding

    for wave_id in wave_totals:
        wave_totals[wave_id]['unpicked'] = max(0, wave_totals[wave_id]['total_volume'] - wave_totals[wave_id]['picked'])

    # 总已拣数量 = G1-G4已拣 + 鸡蛋已拣（从kv_store精确统计）
    c.execute("SELECT value FROM kv_store WHERE key = 'total_all_picked'")
    row_picked = c.fetchone()
    if row_picked:
        totalPicked = int(row_picked['value'])

    # 总未拣数量 = G1+G2+G3+G4的未拣（直接用精确统计值）
    c.execute("SELECT value FROM kv_store WHERE key = 'target_area_unpicked'")
    row_unpicked = c.fetchone()
    if row_unpicked:
        totalUnpicked = int(row_unpicked['value'])

    # 拣货完成状态判断相关计数（用于前端胜利动画 + 弹窗提醒）
    c.execute("SELECT value FROM kv_store WHERE key = 'allocation_created_count'")
    row_alloc = c.fetchone()
    allocation_created_count = int(row_alloc['value']) if row_alloc else 0

    c.execute("SELECT value FROM kv_store WHERE key = 'pick_status_picking_count'")
    row_ps_picking = c.fetchone()
    pick_status_picking_count = int(row_ps_picking['value']) if row_ps_picking else -1

    c.execute("SELECT value FROM kv_store WHERE key = 'pick_status_created_count'")
    row_ps_created = c.fetchone()
    pick_status_created_count = int(row_ps_created['value']) if row_ps_created else -1

    sorted_warehouses = sorted(warehouse_total_vols.keys(), key=lambda w: warehouse_total_vols[w], reverse=True)

    echarts_series_data = {
        '凌晨达': {'p': [], 'u': []},
        '上午达': {'p': [], 'u': []},
        '下午达': {'p': [], 'u': []}
    }
    wave_map = {5: '凌晨达', 1: '上午达', 2: '下午达'}

    for wh in sorted_warehouses:
        waves = warehouse_wave_data.get(wh, {})
        for wave_id, wave_name in wave_map.items():
            d = waves.get(wave_id, {'volume': 0, 'picked': 0})
            p = d['picked']
            u = max(0, d['volume'] - p)
            echarts_series_data[wave_name]['p'].append(p)
            echarts_series_data[wave_name]['u'].append(u)

    now = datetime.now()
    logical_today_dt = now - timedelta(days=1) if now.hour < 8 else now
    logical_date_str = logical_today_dt.strftime("%Y-%m-%d")

    history_dates = [(logical_today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    history_dates.reverse()

    c.execute(f'''
        SELECT work_date, warehouse_name, SUM(bill_count) as daily_vol
        FROM worker_warehouse_wave_bill_count
        WHERE work_date IN ({','.join(['?'] * 7)})
        GROUP BY work_date, warehouse_name
    ''', history_dates)

    history_rows = c.fetchall()
    history_map = {}
    for row in history_rows:
        wh = row['warehouse_name']
        dt = row['work_date']
        vol = row['daily_vol']
        if wh not in history_map:
            history_map[wh] = {}
        history_map[wh][dt] = vol

    total_seasoning_items = sum((w.get('seasoning_area_count') or 0) for w in workers)
    total_feeding_items = sum((w.get('feeding_area_count') or 0) for w in workers)
    total_picked_tasks = sum(worker_tasks_map.values())

    # ===== 今日进度 sparkline + 历史同时段对比 =====
    # 时间范围 18:00~01:30，按工作时间顺序排列
    c.execute('''SELECT time_slot, total_picked FROM picking_progress_snapshot
                 WHERE logical_date = ? ORDER BY time_slot''', (logical_date_str,))
    all_snap = {row["time_slot"]: row["total_picked"] for row in c.fetchall()}
    sparkline_slots = []
    for h in range(18, 24):
        for m in range(0, 60, 10):
            sparkline_slots.append(f"{h:02d}:{m:02d}")
    for h in range(0, 2):
        for m in range(0, 60, 10):
            if h == 1 and m > 20:
                break
            sparkline_slots.append(f"{h:02d}:{m:02d}")
    today_sparkline = [{"t": s, "v": all_snap[s]} for s in sparkline_slots if s in all_snap]

    # 如果今日还没有 sparkline 数据（白天时段），回退显示昨日的趋势线
    sparkline_label = "今日"
    if len(today_sparkline) < 2:
        yesterday_date_str = (logical_today_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        c.execute('''SELECT time_slot, total_picked FROM picking_progress_snapshot
                     WHERE logical_date = ? ORDER BY time_slot''', (yesterday_date_str,))
        yesterday_snap = {row["time_slot"]: row["total_picked"] for row in c.fetchall()}
        yesterday_sparkline = [{"t": s, "v": yesterday_snap[s]} for s in sparkline_slots if s in yesterday_snap]
        if len(yesterday_sparkline) >= 2:
            today_sparkline = yesterday_sparkline
            sparkline_label = "昨日"

    # 历史 7 天同一时段均值（取当前时段 ±10min 内的最近快照）
    now_slot_min = (now.minute // 10) * 10
    current_slot = f"{now.hour:02d}:{now_slot_min:02d}"
    # 白天非工作时段（8:00~17:59），用最后一个工作时段代替
    if 8 <= now.hour < 18:
        current_slot = "01:20"
    hist_7_dates = [(logical_today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]
    c.execute(f'''SELECT logical_date, total_picked FROM picking_progress_snapshot
                  WHERE logical_date IN ({','.join(['?']*7)}) AND time_slot = ?''',
              hist_7_dates + [current_slot])
    hist_same_slot_rows = [(row["logical_date"], row["total_picked"]) for row in c.fetchall() if row["total_picked"] > 0]
    hist_same_slot = [v for _, v in hist_same_slot_rows]
    hist_avg_same_slot = round(sum(hist_same_slot) / len(hist_same_slot)) if hist_same_slot else None

    # 昨日同时段
    yesterday_str = hist_7_dates[0]
    yesterday_same_slot = None
    for d, v in hist_same_slot_rows:
        if d == yesterday_str:
            yesterday_same_slot = v
            break

    # 白天时段对比标签用"最终"而不是"同时段"
    compare_slot_label = current_slot if not (8 <= now.hour < 18) else "最终"

    progress_compare = {
        "current_slot": current_slot,
        "compare_slot_label": compare_slot_label,
        "sparkline_label": sparkline_label,
        "today_picked": totalPicked,
        "hist_avg_same_slot": hist_avg_same_slot,
        "hist_days_count": len(hist_same_slot),
        "diff_pct": round((totalPicked - hist_avg_same_slot) / hist_avg_same_slot * 100, 1) if hist_avg_same_slot else None,
        "yesterday_same_slot": yesterday_same_slot,
        "yesterday_diff_pct": round((totalPicked - yesterday_same_slot) / yesterday_same_slot * 100, 1) if yesterday_same_slot else None,
    }

    conn.close()

    return jsonify({
        "workers": workers,
        "teams": list(teams_set),
        "totalPicked": totalPicked,
        "totalUnpicked": totalUnpicked,
        "total_seasoning_items": total_seasoning_items,
        "total_feeding_items": total_feeding_items,
        "total_picked_tasks": total_picked_tasks,
        "warehouse_seasoning_bills": [totalSeasoningBills],
        "warehouse_feeding_bills": [totalFeedingBills],
        "wave_totals": wave_totals,
        "echarts_y_axis": sorted_warehouses,
        "echarts_series_data": echarts_series_data,
        "logical_date": logical_date_str,
        "history_vol": history_map,
        "history_dates": history_dates,
        "online_workers_count": online_workers_count,
        "participating_count": participating_count,
        "data_update_time": data_update_time,
        "warehouse_area_unpicked": warehouse_area_unpicked,
        "warehouse_wave_need_allot": {
            wh: {str(wid): wd.get('need_allot', 0) for wid, wd in waves.items()}
            for wh, waves in warehouse_wave_data.items()
        },
        "today_sparkline": today_sparkline,
        "progress_compare": progress_compare,
        "allocation_created_count": allocation_created_count,
        "pick_status_picking_count": pick_status_picking_count,
        "pick_status_created_count": pick_status_created_count,
        "warehouse_id": wh_id,
        "warehouse_name": get_warehouse_name(wh_id),
    })


@bp.route('/api/export_worker_data')
def export_worker_data():
    wh_id = _req_warehouse_id()
    conn = database.get_db(wh_id)
    c = conn.cursor()
    today = get_logical_date()

    c.execute('''
        SELECT w.name, w.team_name, w.job_type, w.total_count, w.feeding_area_count, w.seasoning_area_count,
               w.efficiency, w.idle_minutes, w.is_stagnant, w.clockin_time, w.last_modify_time,
               COALESCE(w.seasoning_bill_count, 0) AS seasoning_bill_count,
               COALESCE(w.feeding_bill_count, 0) AS feeding_bill_count
        FROM worker_realtime w
        ORDER BY w.efficiency DESC
    ''')
    workers = c.fetchall()

    c.execute('''
        SELECT name, warehouse_name, SUM(bill_count) as bill_count
        FROM worker_warehouse_wave_bill_count
        WHERE work_date = ?
        GROUP BY name, warehouse_name
    ''', (today,))
    bill_rows = c.fetchall()

    # 查询打卡下班时间
    c.execute('''
        SELECT name, clockout_time FROM daily_attendance_cache
        WHERE attence_date = ?
    ''', (today,))
    clockout_map = {}
    for row in c.fetchall():
        clockout_map[row["name"]] = row["clockout_time"] or ""

    # 查询当天每人停滞次数和总时长（从 picking_stagnant_log 按逻辑日期聚合）
    logical_today_dt = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
    stagnant_start = logical_today_dt.strftime("%Y-%m-%d") + " 06:00:00"
    stagnant_end = (logical_today_dt + timedelta(days=1)).strftime("%Y-%m-%d") + " 08:00:00"
    c.execute('''
        SELECT handler_name,
               COUNT(*) as stagnant_count,
               SUM(CASE WHEN stagnant_duration_minutes > 0 THEN stagnant_duration_minutes ELSE stagnant_minutes END) as total_duration
        FROM picking_stagnant_log
        WHERE record_time >= ? AND record_time < ?
          AND (CASE WHEN stagnant_duration_minutes > 0 THEN stagnant_duration_minutes ELSE stagnant_minutes END) >= 10
        GROUP BY handler_name
    ''', (stagnant_start, stagnant_end))
    stagnant_stats = {}
    for row in c.fetchall():
        stagnant_stats[row["handler_name"]] = {
            "count": row["stagnant_count"],
            "duration": round(row["total_duration"] or 0, 1),
        }

    conn.close()

    worker_bills = {}
    for row in bill_rows:
        name = row['name']
        if name not in worker_bills:
            worker_bills[name] = []
        worker_bills[name].append(f"{row['warehouse_name']}:{row['bill_count']}")

    def fmt_time(dt_str):
        """只保留时:分"""
        if not dt_str:
            return ''
        try:
            # 格式可能是 "2026-06-15 18:30:00" 或 datetime
            if ' ' in str(dt_str):
                return str(dt_str).split(' ')[1][:5]
            return str(dt_str)[:5]
        except Exception:
            return str(dt_str)

    def calc_attendance_hours(clockin_str, clockout_str):
        """计算出勤时长（小时），支持跨天"""
        if not clockin_str or not clockout_str:
            return 0
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            ci = datetime.strptime(str(clockin_str)[:19], fmt)
            co = datetime.strptime(str(clockout_str)[:19], fmt)
            if co < ci:
                co += timedelta(days=1)
            return (co - ci).total_seconds() / 3600
        except (ValueError, TypeError):
            return 0

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['姓名', '班组', '上班打卡', '最后完成', '下班打卡', '总件数', '米面件数', '上料件数', '米面单数', '上料单数', '直接效率(件/时)', '综合效率(件/时)', '停顿时长(分)', '是否停滞', '停滞次数', '停滞总时长(分)', '各仓单量'])
    for w in workers:
        bills_text = ' '.join(worker_bills.get(w['name'], []))
        team_short = _short_team(w['team_name'] or '')
        wage_label = _wage_type_label(w['job_type'])
        if wage_label:
            team_short = f"{wage_label} {team_short}" if team_short else wage_label
        st = stagnant_stats.get(w['name'], {})
        # 直接效率 = 总件数 / 拣货任务总时长（各任务 completeTime - acceptTime 之和）
        direct_eff = round(w['efficiency'] or 0, 1)
        # 综合效率 = 总件数 / 出勤时长（下班打卡 - 上班打卡）
        clockout_time_str = clockout_map.get(w['name'], '')
        attendance_hours = calc_attendance_hours(w['clockin_time'], clockout_time_str)
        if attendance_hours > 0 and (w['total_count'] or 0) > 0:
            overall_eff = round(w['total_count'] / attendance_hours, 1)
        else:
            overall_eff = ''
        writer.writerow([
            w['name'], team_short,
            fmt_time(w['clockin_time']),
            fmt_time(w['last_modify_time']),
            fmt_time(clockout_time_str),
            w['total_count'],
            w['seasoning_area_count'], w['feeding_area_count'],
            w['seasoning_bill_count'], w['feeding_bill_count'],
            direct_eff, overall_eff,
            round(w['idle_minutes'] or 0, 1),
            '是' if w['is_stagnant'] else '否',
            st.get('count', 0), st.get('duration', 0),
            bills_text
        ])

    output.seek(0)
    return Response(
        '\ufeff' + output.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename=worker_data_{today}.csv'}
    )


@bp.route('/api/picking_stagnant')
def get_picking_stagnant():
    """查询当前未解除的拣货卡单（含员工班组/临时工标识）"""
    wh_id = _req_warehouse_id()
    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('''
        SELECT s.id, s.record_time, s.handler_name, s.zone_pick_bill_no,
               s.logic_area_name, s.biz_bill_name, s.order_unit_qty, s.actual_unit_qty,
               s.pick_status, s.last_modify_time, s.stagnant_minutes, s.alert_reason,
               s.is_resolved, s.resolve_time,
               w.team_name, w.is_temp_worker
        FROM picking_stagnant_log s
        LEFT JOIN worker_realtime w ON s.handler_name = w.name
        WHERE s.is_resolved = 0
        ORDER BY s.stagnant_minutes DESC
    ''')
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(rows)


@bp.route('/api/picking_stagnant/history')
def get_picking_stagnant_history():
    """查询今日所有卡单记录（含已解除）"""
    wh_id = _req_warehouse_id()
    today = get_logical_date()
    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('''
        SELECT id, record_time, handler_name, zone_pick_bill_no,
               logic_area_name, biz_bill_name, order_unit_qty, actual_unit_qty,
               pick_status, last_modify_time, stagnant_minutes, is_resolved, resolve_time
        FROM picking_stagnant_log
        WHERE DATE(record_time) = ?
        ORDER BY record_time DESC
    ''', (today,))
    rows = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(rows)


@bp.route('/api/exclude_worker', methods=['POST'])
def exclude_worker():
    """标记 / 取消标记非拣货人员"""
    data = request.get_json(force=True)
    name = (data or {}).get('name', '').strip()
    exclude = data.get('exclude', True)
    if not name:
        return jsonify({'ok': False, 'msg': '缺少人员姓名'}), 400
    wh_id = _req_warehouse_id()
    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('UPDATE worker_info_cache SET is_excluded = ? WHERE name = ?',
              (1 if exclude else 0, name))
    conn.commit()
    affected = c.rowcount
    conn.close()
    if affected == 0:
        return jsonify({'ok': False, 'msg': f'未找到人员: {name}'}), 404
    action = '已标记为非拣货人员' if exclude else '已取消标记'
    return jsonify({'ok': True, 'msg': f'{name} {action}'})


# ===== 拣货中任务详情 API =====
@bp.route('/api/picking_in_progress')
def api_picking_in_progress():
    """实时查询 klwms 拣货中(picking)的子任务列表，按人员分组统计进度"""
    from spiders.base import request_with_retry, get_shared_session
    from spiders.picking import FEEDING_AREAS, SEASONING_AREAS, TARGET_AREAS
    wh_id = _req_warehouse_id()
    session = get_shared_session()
    logical_today = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
    today_date = logical_today.strftime("%Y-%m-%d")
    tomorrow_date = (logical_today + timedelta(days=1)).strftime("%Y-%m-%d")

    api_pick = "/haina/outbound/zonepick/r/pageList"
    params = {
        "createTimeStart": f"{today_date} 06:00:00",
        "createTimeEnd": f"{tomorrow_date} 12:00:00",
        "pickStatus": "picking",
        "billType": "DB",
        "deliveryRegionIds": "",
        "pageNo": 1,
        "pageSize": 1000,
        "wareHouseId": wh_id,
        "warehouseId": wh_id,
    }

    res = request_with_retry(session, api_pick, params)
    if not res:
        return jsonify({"ok": False, "msg": "接口请求失败"})

    records = res.get("data", {}).get("pageContent", [])

    # 统计概览
    total_tasks = 0
    total_order_qty = 0
    total_actual_qty = 0
    seasoning_order_qty = 0
    seasoning_actual_qty = 0
    feeding_order_qty = 0
    feeding_actual_qty = 0

    # 按履约波次分组统计
    wave_stats = {}

    # 按人员分组
    worker_progress = {}

    for rec in records:
        area = rec.get("logicAreaName", "")
        if area not in TARGET_AREAS:
            continue

        total_tasks += 1
        order_qty = rec.get("orderUnitTotalQty") or 0
        actual_qty = rec.get("actualUnitTotalQty") or 0
        total_order_qty += order_qty
        total_actual_qty += actual_qty

        # 按履约波次统计（凌晨达/上午达等）
        wave_name = rec.get("fulfillmentWaveName", "") or "未知波次"
        if wave_name not in wave_stats:
            wave_stats[wave_name] = {"name": wave_name, "task_count": 0, "order_qty": 0, "actual_qty": 0}
        wave_stats[wave_name]["task_count"] += 1
        wave_stats[wave_name]["order_qty"] += order_qty
        wave_stats[wave_name]["actual_qty"] += actual_qty

        if area in SEASONING_AREAS:
            seasoning_order_qty += order_qty
            seasoning_actual_qty += actual_qty
            area_type = "seasoning"
        elif area in FEEDING_AREAS:
            feeding_order_qty += order_qty
            feeding_actual_qty += actual_qty
            area_type = "feeding"
        else:
            area_type = "other"

        handler = rec.get("handlerName", "未知")
        if handler not in worker_progress:
            worker_progress[handler] = {
                "name": handler,
                "tasks": [],
                "total_order": 0,
                "total_actual": 0,
                "seasoning_order": 0,
                "seasoning_actual": 0,
                "feeding_order": 0,
                "feeding_actual": 0,
            }
        wp = worker_progress[handler]
        wp["total_order"] += order_qty
        wp["total_actual"] += actual_qty
        if area_type == "seasoning":
            wp["seasoning_order"] += order_qty
            wp["seasoning_actual"] += actual_qty
        elif area_type == "feeding":
            wp["feeding_order"] += order_qty
            wp["feeding_actual"] += actual_qty

        wp["tasks"].append({
            "zonePickBillNo": rec.get("zonePickBillNo", ""),
            "bizBillName": rec.get("bizBillName", ""),
            "logicAreaName": area,
            "orderQty": order_qty,
            "actualQty": actual_qty,
            "areaType": area_type,
            "fulfillmentWaveName": rec.get("fulfillmentWaveName", ""),
        })

    # 查询人员临时工标记
    wh_id = _req_warehouse_id()
    conn = database.get_db(wh_id)
    c = conn.cursor()
    worker_names = list(worker_progress.keys())
    for name in worker_names:
        c.execute("SELECT is_temp_worker FROM worker_info_cache WHERE name = ?", (name,))
        row = c.fetchone()
        worker_progress[name]["is_temp_worker"] = bool(row["is_temp_worker"]) if row else False
    conn.close()

    # 按完成度升序排列（完成少的在前，方便关注进度落后的人员）
    workers_list = sorted(worker_progress.values(), key=lambda x: (x["total_actual"] / x["total_order"]) if x["total_order"] > 0 else 0)
    # 计算每人逻辑区标签 & 移除tasks明细减少传输量
    # 每人同时只有一条拣货中任务，直接取该任务的逻辑区简称
    AREA_SHORT = {
        "上料小件G1拣货区": "上料G1",
        "爆品饮料G4拣货区": "上料G4",
        "酒饮米面G2拣货区": "米面G2",
        "爆品米面G3拣货区": "米面G3",
    }
    for w in workers_list:
        w["task_count"] = len(w["tasks"])
        # 取唯一任务的逻辑区和仓名
        if w["tasks"]:
            area_full = w["tasks"][0].get("logicAreaName", "")
            w["area_label"] = AREA_SHORT.get(area_full, area_full)
            w["warehouse"] = w["tasks"][0].get("bizBillName", "")
            w["wave_name"] = w["tasks"][0].get("fulfillmentWaveName", "")
        else:
            w["area_label"] = ""
            w["warehouse"] = ""
            w["wave_name"] = ""
        del w["tasks"]

    # 波次列表按固定履约顺序：凌晨达 → 上午达 → 下午达
    _wave_order = {"凌晨达": 0, "上午达": 1, "下午达": 2}
    waves_list = sorted(wave_stats.values(), key=lambda x: _wave_order.get(x["name"], 99))

    return jsonify({
        "ok": True,
        "overview": {
            "total_tasks": total_tasks,
            "total_order_qty": total_order_qty,
            "total_actual_qty": total_actual_qty,
            "seasoning_order_qty": seasoning_order_qty,
            "seasoning_actual_qty": seasoning_actual_qty,
            "feeding_order_qty": feeding_order_qty,
            "feeding_actual_qty": feeding_actual_qty,
            "worker_count": len(worker_progress),
        },
        "waves": waves_list,
        "workers": workers_list,
    })


# ===== 待拣已生成 - 子任务列表 =====
@bp.route('/created_bills')
def created_bills_page():
    return render_template('created_bills.html')


# ===== 任务包裹明细页面 =====
@bp.route('/task_detail')
def task_detail_page():
    """任务包裹明细页面：按子任务号查询包裹列表"""
    return render_template('task_detail.html')


@bp.route('/api/task_detail')
def api_task_detail():
    """按 zonePickBillNo 查询该子任务的包裹打印列表"""
    from spiders.base import request_with_retry, get_shared_session
    wh_id = _req_warehouse_id()
    zone_pick_bill_no = request.args.get('zonePickBillNo', '').strip()
    allot_in_wh_id = request.args.get('allotInWarehouseId', '').strip()
    appointment_time = request.args.get('appointmentTime', '').strip()

    if not zone_pick_bill_no:
        return jsonify({"ok": False, "msg": "缺少子任务号"})

    if not allot_in_wh_id:
        return jsonify({"ok": False, "msg": "缺少调入仓库ID（该仓库可能未在映射表中）"})

    session = get_shared_session()

    # 如果没传日期，用逻辑日期
    if not appointment_time:
        logical_today = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
        appointment_time = logical_today.strftime("%Y-%m-%d")

    params = {
        "warehouseId": wh_id,
        "wareHouseId": wh_id,
        "appointmentTime": appointment_time,
        "allotInWarehouseId": allot_in_wh_id,
        "zonePickBillNo": zone_pick_bill_no,
        "containerPrintTaskStatus": "",
        "fulfillmentWaveId": "",
        "isFulfillmentWaveGray": "true",
        "orderBy": "",
        "asc": "true",
        "pageNo": 1,
        "pageSize": 200,
    }

    res = request_with_retry(session, "/haina/ojs/rdc/r/containerParcelPrintList", params)
    if not res:
        return jsonify({"ok": False, "msg": "接口请求失败，Token 可能已过期"})

    if res.get("code") == 500:
        return jsonify({"ok": False, "msg": f"查询失败: {res.get('message', '未知错误')}"})

    page_content = res.get("data", {}).get("pageContent", [])

    # 格式化时间戳
    for item in page_content:
        for field in ("createTime", "printTime", "appointmentTime"):
            raw = item.get(field)
            if isinstance(raw, (int, float)) and raw > 0:
                item[field] = datetime.fromtimestamp(raw / 1000).strftime("%Y-%m-%d %H:%M:%S")

    return jsonify({
        "ok": True,
        "data": page_content,
        "total": len(page_content),
        "zonePickBillNo": zone_pick_bill_no,
    })


@bp.route('/api/task_detail_parcels')
def api_task_detail_parcels():
    """查询子任务的包裹 SKU 明细（通过 containerParcelPrintQuery）"""
    from spiders.base import request_with_retry, get_shared_session
    wh_id = _req_warehouse_id()
    zone_pick_bill_no = request.args.get('zonePickBillNo', '').strip()
    allot_in_wh_id = request.args.get('allotInWarehouseId', '').strip()
    appointment_time = request.args.get('appointmentTime', '').strip()

    if not zone_pick_bill_no:
        return jsonify({"ok": False, "msg": "缺少子任务号"})
    if not allot_in_wh_id:
        return jsonify({"ok": False, "msg": "缺少调入仓库ID"})

    session = get_shared_session()

    # 如果没传日期，用逻辑日期
    if not appointment_time:
        logical_today = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
        appointment_time = logical_today.strftime("%Y-%m-%d")

    # 第一步：通过 containerParcelPrintList 获取 containerPrintTaskNos
    list_params = {
        "warehouseId": wh_id,
        "wareHouseId": wh_id,
        "appointmentTime": appointment_time,
        "allotInWarehouseId": allot_in_wh_id,
        "zonePickBillNo": zone_pick_bill_no,
        "containerPrintTaskStatus": "",
        "fulfillmentWaveId": "",
        "isFulfillmentWaveGray": "true",
        "orderBy": "",
        "asc": "true",
        "pageNo": 1,
        "pageSize": 200,
    }
    list_res = request_with_retry(session, "/haina/ojs/rdc/r/containerParcelPrintList", list_params)
    if not list_res or list_res.get("code") != 200:
        return jsonify({"ok": False, "msg": "查询打印任务列表失败"})

    page_content = list_res.get("data", {}).get("pageContent", [])
    if not page_content:
        return jsonify({"ok": False, "msg": "未找到打印任务"})

    nos = [item.get("containerPrintTaskNo", "") for item in page_content if item.get("containerPrintTaskNo")]
    task_nos = ",".join(nos)
    if not task_nos:
        return jsonify({"ok": False, "msg": "未找到打印任务号"})

    # 获取 labelPrintTiming
    label_print_timing = "BEFORE_PICKING"
    if page_content[0].get("labelPrintTiming"):
        label_print_timing = page_content[0]["labelPrintTiming"]

    # 第二步：通过 containerParcelPrintQuery 获取包裹明细
    query_params = {
        "warehouseId": wh_id,
        "wareHouseId": wh_id,
        "allotInWarehouseId": allot_in_wh_id,
        "containerPrintTaskNos": task_nos,
        "labelPrintTiming": label_print_timing,
    }
    res = request_with_retry(session, "/haina/ojs/rdc/r/containerParcelPrintQuery", query_params)
    if not res:
        return jsonify({"ok": False, "msg": "查询包裹明细失败，Token 可能已过期"})

    print_data = res.get("data", [])
    if not print_data:
        return jsonify({"ok": False, "msg": "未查询到包裹明细数据"})

    # 提取包裹明细
    parcels = []
    for task in print_data:
        tags = task.get("packageTags", [])
        for tag in tags:
            parcels.append({
                "packageNo": tag.get("packageNo", ""),
                "skuName": tag.get("skuName", ""),
                "skuUnitDesc": tag.get("skuUnitDesc", ""),
                "orderNo": tag.get("orderNo", ""),
                "customerName": tag.get("customerName", ""),
                "areaNo": tag.get("areaNo", ""),
                "stationAreaNo": tag.get("stationAreaNo", ""),
                "stationRouteSeq": tag.get("stationRouteSeq", ""),
                "poiSerialCode": tag.get("poiSerialCode", ""),
                "transportCategoryAbbr": tag.get("transportCategoryAbbr", ""),
                "warehouseShortName": tag.get("warehouseShortName", ""),
            })

    return jsonify({
        "ok": True,
        "data": parcels,
        "total": len(parcels),
        "zonePickBillNo": zone_pick_bill_no,
    })


@bp.route('/api/created_bills')
def api_created_bills():
    """实时查询 klwms 已生成(created)的拣货子任务列表"""
    from spiders.base import request_with_retry, get_shared_session
    wh_id = _req_warehouse_id()
    session = get_shared_session()
    logical_today = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
    today_date = logical_today.strftime("%Y-%m-%d")
    tomorrow_date = (logical_today + timedelta(days=1)).strftime("%Y-%m-%d")

    api_pick = "/haina/outbound/zonepick/r/pageList"
    base_params = {
        "createTimeStart": f"{today_date} 06:00:00",
        "createTimeEnd": f"{tomorrow_date} 12:00:00",
        "billType": "DB",
        "deliveryRegionIds": "",
        "pageSize": 1000,
        "pickStatus": "created",
        "wareHouseId": wh_id,
        "warehouseId": wh_id,
    }

    all_records = []
    pg = 1
    while True:
        params = {**base_params, "pageNo": pg}
        res = request_with_retry(session, api_pick, params)
        if not res:
            break
        page_content = res.get("data", {}).get("pageContent", [])
        if not page_content:
            break
        all_records.extend(page_content)
        total_pages = res.get("data", {}).get("page", {}).get("totalPageCount", 1)
        if pg >= total_pages:
            break
        pg += 1

    # 仓库名称 → ID 映射（用于 containerParcelPrintList 查询）
    WH_NAME_TO_ID = {
        '深圳平湖仓': '100', '深圳光明仓': '189', '惠州惠城仓': '349',
        '深圳龙华二仓': '479', '深圳福永仓': '501', '东莞虎门仓': '546',
        '深圳南山仓': '557', '东莞信立仓': '616', '深圳清溪仓': '636',
        '惠州河源站（配送商）': '782', '惠州汕尾站（配送商）': '783',
    }

    # 提取需要的字段，按打印时间倒序
    bills = []
    for r in all_records:
        zone_pick_bill_no = r.get("zonePickBillNo", "")
        biz_bill_name = r.get("bizBillName", "")  # 所属仓（即调入仓名称）
        logic_area = r.get("logicAreaName", "")  # 逻辑区
        # createTime 是毫秒时间戳，转为可读字符串
        raw_time = r.get("createTime")
        if isinstance(raw_time, (int, float)) and raw_time > 0:
            create_time = datetime.fromtimestamp(raw_time / 1000).strftime("%Y-%m-%d %H:%M:%S")
        elif isinstance(raw_time, str) and raw_time:
            create_time = raw_time
        else:
            create_time = ""
        fulfillment_wave = r.get("fulfillmentWaveName", "")  # 波次
        allot_in_wh_id = WH_NAME_TO_ID.get(biz_bill_name, "")  # 目的仓ID
        # appointmentDate 是履约日期（毫秒时间戳），用于 containerParcelPrintList 查询
        raw_appt = r.get("appointmentDate")
        if isinstance(raw_appt, (int, float)) and raw_appt > 0:
            appointment_date = datetime.fromtimestamp(raw_appt / 1000).strftime("%Y-%m-%d")
        else:
            appointment_date = ""
        parcel_qty = r.get("orderUnitTotalQty") or r.get("parcelQty") or r.get("skuQty") or 0
        sku_qty = r.get("skuQty") or 0
        bills.append({
            "zonePickBillNo": zone_pick_bill_no,
            "bizBillName": biz_bill_name,
            "logicAreaName": logic_area,
            "createTime": create_time,
            "fulfillmentWaveName": fulfillment_wave,
            "allotInWarehouseId": allot_in_wh_id,
            "appointmentDate": appointment_date,
            "parcelQty": parcel_qty,
            "skuQty": sku_qty,
        })

    # 按创建时间倒序
    bills.sort(key=lambda x: x.get("createTime", ""), reverse=True)

    return jsonify({
        "ok": True,
        "data": bills,
        "total": len(bills),
        "appointmentTime": today_date,
        "warehouseId": wh_id,
    })


@bp.route('/api/print_label', methods=['POST'])
def api_print_label():
    """
    直接打印标签 - 支持两种调用方式：
    1. 传 containerPrintTaskNos（直接打印指定任务）
    2. 传 zonePickBillNo（自动查询任务号再打印）
    POST body: { zonePickBillNo: "DBJ...", allotInWarehouseId: "100",
                 appointmentTime: "2026-05-31", fulfillmentWaveId: "5" }
    """
    from spiders.base import request_with_retry, get_shared_session
    import label_printer
    wh_id = _req_warehouse_id()
    data = request.get_json(force=True) if request.is_json else request.form.to_dict()
    task_nos = data.get('containerPrintTaskNos', '').strip()
    zone_pick_bill_no = data.get('zonePickBillNo', '').strip()
    allot_in_wh_id = data.get('allotInWarehouseId', '').strip()
    appointment_time = data.get('appointmentTime', '').strip()
    fulfillment_wave_id = data.get('fulfillmentWaveId', '').strip()

    # 如果没传日期，用逻辑日期
    if not appointment_time:
        logical_today = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
        appointment_time = logical_today.strftime("%Y-%m-%d")

    session = get_shared_session()

    # 如果没有直接传 containerPrintTaskNos，则通过 zonePickBillNo 查询
    label_print_timing = "BEFORE_PICKING"  # 默认值
    if not task_nos and zone_pick_bill_no:
        list_params = {
            "warehouseId": wh_id,
            "wareHouseId": wh_id,
            "appointmentTime": appointment_time,
            "allotInWarehouseId": allot_in_wh_id,
            "zonePickBillNo": zone_pick_bill_no,
            "containerPrintTaskStatus": "",
            "fulfillmentWaveId": "",
            "isFulfillmentWaveGray": "true",
            "orderBy": "",
            "asc": "true",
            "pageNo": 1,
            "pageSize": 200,
        }
        list_res = request_with_retry(session, "/haina/ojs/rdc/r/containerParcelPrintList", list_params)
        if list_res and list_res.get("code") == 200:
            page_content = list_res.get("data", {}).get("pageContent", [])
            nos = [item.get("containerPrintTaskNo", "") for item in page_content if item.get("containerPrintTaskNo")]
            task_nos = ",".join(nos)
            # 从返回数据中获取 labelPrintTiming
            if page_content and page_content[0].get("labelPrintTiming"):
                label_print_timing = page_content[0]["labelPrintTiming"]
        elif list_res and list_res.get("code") == 500:
            return jsonify({"ok": False, "msg": f"查询失败: {list_res.get('message', '未知错误')}"})

    if not task_nos:
        return jsonify({"ok": False, "msg": "未找到打印任务号，该子任务可能尚未生成包裹（需要装箱后才能打印）"})

    # 调用 klwms API 获取打印数据
    params = {
        "warehouseId": wh_id,
        "wareHouseId": wh_id,
        "allotInWarehouseId": allot_in_wh_id,
        "containerPrintTaskNos": task_nos,
        "labelPrintTiming": label_print_timing,
    }

    res = request_with_retry(session, "/haina/ojs/rdc/r/containerParcelPrintQuery", params)
    if not res:
        return jsonify({"ok": False, "msg": "获取打印数据失败，Token 可能已过期"})

    print_data = res.get("data", [])
    if not print_data:
        return jsonify({"ok": False, "msg": "未查询到打印数据"})

    # 渲染标签
    try:
        images = label_printer.render_all_labels(print_data)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"标签渲染失败: {str(e)}"})

    # 发送到打印机
    try:
        result = label_printer.print_labels_win32(images)
    except Exception as e:
        return jsonify({"ok": False, "msg": f"打印失败: {str(e)}"})

    return jsonify({
        "ok": result["printed"] > 0,
        "msg": f"成功打印 {result['printed']}/{result['total']} 张标签",
        "detail": result,
    })


@bp.route('/api/open_print_preview', methods=['POST'])
def api_open_print_preview():
    """
    通过浏览器自动化打开 klwms 打印预览弹窗。
    POST body: { zonePickBillNo, allotInWarehouseId, appointmentTime }
    流程：导航到 shareAllotPrint → 填入搜索条件 → 查询 → 勾选目标行 → 点击"打印包裹标签"
    """
    import subprocess, json as _json

    data = request.get_json(force=True) if request.is_json else request.form.to_dict()
    zone_pick_bill_no = data.get('zonePickBillNo', '').strip()
    allot_in_wh_id = data.get('allotInWarehouseId', '').strip()
    appointment_time = data.get('appointmentTime', '').strip()

    if not zone_pick_bill_no:
        return jsonify({"ok": False, "msg": "缺少 zonePickBillNo"})

    if not appointment_time:
        logical_today = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
        appointment_time = logical_today.strftime("%Y-%m-%d")

    CATDESK_CMD = r"C:\Users\程旭同\.catdesk\bin\catdesk.cmd"

    def _run_browser(action_json):
        """执行 catdesk browser-action 并返回解析后的 JSON"""
        action_str = _json.dumps(action_json, ensure_ascii=False)
        result = subprocess.run(
            [CATDESK_CMD, "browser-action", action_str],
            capture_output=True, text=True, timeout=30, shell=True
        )
        if result.returncode != 0:
            return None
        try:
            return _json.loads(result.stdout)
        except Exception:
            return None

    try:
        import logging
        logger = logging.getLogger(__name__)
        debug_info = []  # 收集调试信息

        # 1. 导航到打印页面
        nav_url = "https://klwms.meituan.com/app/haina/stockOut/shareAllotPrint"
        nav_res = _run_browser({"action": "navigate", "url": nav_url, "waitUntil": "networkidle"})
        if not nav_res or not nav_res.get("success"):
            return jsonify({"ok": False, "msg": "无法打开 klwms 打印页面", "debug": str(nav_res)})

        # 等待页面完全渲染
        _run_browser({"action": "wait", "timeout": 2000})

        # 2. 获取页面快照，分析表单结构
        snap = _run_browser({"action": "snapshot", "interactive": True})
        if not snap or not snap.get("success"):
            return jsonify({"ok": False, "msg": "无法获取页面快照"})

        # 3. 从 snapshot 定位"储位拣货子任务"输入框
        #    经实际测试验证的页面结构：
        #    snapshot text 中 textbox 有两种格式：
        #      - 有值: "textbox [ref=eXX]: 值" 或 "textbox "请选择" [ref=eXX]: 值"
        #      - 无值: "textbox [ref=eXX]"（没有冒号和值）
        #    容器号和储位拣货子任务都是无值的 textbox，在 snapshot text 中紧挨着出现
        #    容器号是第1个无值 textbox，储位拣货子任务是第2个无值 textbox
        import re
        refs = snap.get("data", {}).get("refs", {})
        target_ref = None

        # 注意：snapshot 数据中 text 字段名为 "snapshot"（不是 "text"）
        snap_text = snap.get("data", {}).get("snapshot", "") or snap.get("data", {}).get("text", "")

        # 策略A（最可靠）：从 snapshot text 中找"无值 textbox"
        # 有值的 textbox 格式: "textbox [ref=eXX]: 值" 或 "textbox "xxx" [ref=eXX]: 值"
        # 无值的 textbox 格式: "textbox [ref=eXX]"（行尾或后面紧跟换行）
        # 匹配无值 textbox：textbox 后面直接跟 [ref=eXX] 且后面没有冒号
        empty_textbox_refs = re.findall(r'- textbox \[ref=(e\d+)\](?:\s*$|\n)', snap_text, re.MULTILINE)

        if empty_textbox_refs:
            debug_info.append(f"empty_textboxes_in_text: {empty_textbox_refs}")
            # 第1个是容器号，第2个是储位拣货子任务
            if len(empty_textbox_refs) >= 2:
                target_ref = empty_textbox_refs[1]
            elif len(empty_textbox_refs) == 1:
                target_ref = empty_textbox_refs[0]
            debug_info.append(f"target_ref={target_ref}")

        # 策略B（兜底）：从 refs 中找 name 为空的 textbox，按 ref 编号排序取第2个
        if not target_ref:
            all_textboxes = sorted(
                [k for k, v in refs.items() if v.get("role") == "textbox"],
                key=lambda x: int(x[1:])
            )
            # name 为空且不是"请选择"（下拉框）的 textbox
            empty_textboxes = [k for k in all_textboxes 
                             if not refs[k].get("name") or refs[k].get("name") == ""]
            debug_info.append(f"fallback: all_textboxes={all_textboxes}, empty={empty_textboxes}")
            # 需要排除有值的（在 snapshot text 中有冒号后面跟值的）
            # 简单策略：取倒数第2个和倒数第1个空 textbox 中的后一个
            if len(empty_textboxes) >= 2:
                # 容器号和储位拣货子任务是连续的两个空 textbox
                # 从 snapshot text 中确认它们的顺序
                target_ref = empty_textboxes[1] if len(empty_textboxes) <= 3 else empty_textboxes[2]
            elif len(empty_textboxes) == 1:
                target_ref = empty_textboxes[0]
            debug_info.append(f"fallback target_ref={target_ref}")

        # 策略C（最终兜底）：直接用 JS 通过 placeholder 定位
        if not target_ref:
            fill_script = f"""
            (function() {{
                // 通过 placeholder 或 label 文本定位"储位拣货子任务"输入框
                const inputs = document.querySelectorAll('input');
                for (const inp of inputs) {{
                    const placeholder = inp.getAttribute('placeholder') || '';
                    if (placeholder.includes('拣货子任务') || placeholder.includes('子任务')) {{
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(inp, '{zone_pick_bill_no}');
                        inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return 'filled_by_placeholder';
                    }}
                }}
                // 通过 label 文本定位
                const labels = document.querySelectorAll('[class*="label"], label, th, span');
                for (const lbl of labels) {{
                    if (lbl.textContent && lbl.textContent.includes('拣货子任务')) {{
                        const row = lbl.closest('tr, [class*="row"], [class*="form-item"]');
                        if (row) {{
                            const inp = row.querySelector('input');
                            if (inp) {{
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                setter.call(inp, '{zone_pick_bill_no}');
                                inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                return 'filled_by_label';
                            }}
                        }}
                    }}
                }}
                return 'not_found';
            }})()
            """
            _run_browser({"action": "evaluate", "script": fill_script})
        else:
            # 经测试验证：click + fill 即可成功填入值
            # 注意：snapshot 的 name 字段不反映实际输入值，但截图确认 fill 是有效的
            debug_info.append(f"filling @{target_ref} with {zone_pick_bill_no}")

            # 点击输入框聚焦
            _run_browser({"action": "click", "selector": f"@{target_ref}"})
            _run_browser({"action": "wait", "timeout": 300})

            # fill 填入值
            fill_res = _run_browser({"action": "fill", "selector": f"@{target_ref}", "value": zone_pick_bill_no})
            debug_info.append(f"fill result: {fill_res}")
            _run_browser({"action": "wait", "timeout": 300})

        # 4. 找到"查询"按钮并点击
        #    注意：snapshot 的 name 字段不反映输入值，所以不做 fill 验证
        #    直接用初始 snapshot 的 refs 查找按钮（ref 编号在同一页面生命周期内不变）
        query_btn = None
        for k, v in refs.items():
            if v.get("role") == "button" and "查询" in (v.get("name") or ""):
                query_btn = k
                break
        if not query_btn:
            # 从 snapshot text 中找"查询"按钮
            query_match = re.search(r'button\s+"查询"\s*\[ref=(e\d+)\]', snap_text)
            if query_match:
                query_btn = query_match.group(1)

        debug_info.append(f"query_btn: {query_btn}")
        if query_btn:
            _run_browser({"action": "click", "selector": f"@{query_btn}"})
        else:
            # 兜底：用文本选择器
            _run_browser({"action": "click", "selector": "text=查询"})

        # 等待查询结果加载（网络请求可能较慢）
        _run_browser({"action": "wait", "timeout": 5000})

        # 5. 重新快照，找到 checkbox 并全选
        snap2 = _run_browser({"action": "snapshot", "interactive": True})
        if not snap2 or not snap2.get("success"):
            return jsonify({"ok": False, "msg": "查询后无法获取页面"})

        refs2 = snap2.get("data", {}).get("refs", {})

        # 找到表头的全选 checkbox（通常是第一个 checkbox）
        checkboxes = sorted(
            [k for k, v in refs2.items() if v.get("role") == "checkbox"],
            key=lambda x: int(x[1:])
        )
        if not checkboxes:
            return jsonify({"ok": False, "msg": "未找到勾选框，可能查询无结果", "debug": debug_info})

        debug_info.append(f"checkboxes found: {checkboxes[:5]}")
        # 点击第一个 checkbox（全选）— 这是表头的全选框
        _run_browser({"action": "click", "selector": f"@{checkboxes[0]}"})
        _run_browser({"action": "wait", "timeout": 1000})

        # 6. 找到"打印包裹标签"按钮并点击
        print_btn = None
        for k, v in refs2.items():
            if v.get("role") == "button" and "打印包裹标签" in (v.get("name") or ""):
                print_btn = k
                break

        if not print_btn:
            # 重新快照（勾选后按钮可能才出现/变化）
            snap3 = _run_browser({"action": "snapshot", "interactive": True})
            if snap3 and snap3.get("success"):
                refs3 = snap3.get("data", {}).get("refs", {})
                for k, v in refs3.items():
                    if v.get("role") == "button" and "打印包裹标签" in (v.get("name") or ""):
                        print_btn = k
                        refs2 = refs3  # 使用新的 refs
                        break

        if not print_btn:
            return jsonify({"ok": False, "msg": "未找到'打印包裹标签'按钮", "debug": debug_info})

        debug_info.append(f"print_btn: {print_btn}")
        _run_browser({"action": "click", "selector": f"@{print_btn}"})

        # 等待弹窗出现
        _run_browser({"action": "wait", "timeout": 3000})

        return jsonify({"ok": True, "msg": "已打开打印预览弹窗", "debug": debug_info})

    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "msg": "浏览器操作超时"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"操作失败: {str(e)}"})


@bp.route('/api/printer_status')
def api_printer_status():
    """检查打印机状态"""
    import label_printer
    printer_name = request.args.get('printer', label_printer.PRINTER_NAME)
    status = label_printer.check_printer_status(printer_name)
    printers = label_printer.get_available_printers()
    return jsonify({
        "ok": True,
        "printer": status,
        "available_printers": printers,
    })


@bp.route('/api/preview_label')
def api_preview_label():
    """预览标签（返回 PNG 图片）- 用于调试"""
    from spiders.base import request_with_retry, get_shared_session
    import label_printer
    wh_id = _req_warehouse_id()
    task_nos = request.args.get('containerPrintTaskNos', '').strip()
    allot_in_wh_id = request.args.get('allotInWarehouseId', '').strip()
    appointment_time = request.args.get('appointmentTime', '').strip()
    label_index = int(request.args.get('index', '0'))

    if not task_nos:
        return jsonify({"ok": False, "msg": "缺少 containerPrintTaskNos"})

    if not appointment_time:
        logical_today = datetime.now() if datetime.now().hour >= 8 else datetime.now() - timedelta(days=1)
        appointment_time = logical_today.strftime("%Y-%m-%d")

    session = get_shared_session()
    params = {
        "warehouseId": wh_id,
        "wareHouseId": wh_id,
        "allotInWarehouseId": allot_in_wh_id,
        "containerPrintTaskNos": task_nos,
        "labelPrintTiming": "BEFORE_PICKING",
    }

    res = request_with_retry(session, "/haina/ojs/rdc/r/containerParcelPrintQuery", params)
    if not res:
        return jsonify({"ok": False, "msg": "获取打印数据失败"})

    print_data = res.get("data", [])
    if not print_data:
        return jsonify({"ok": False, "msg": "无数据"})

    images = label_printer.render_all_labels(print_data)
    if label_index >= len(images):
        return jsonify({"ok": False, "msg": f"索引超出范围，共 {len(images)} 张"})

    # 返回 PNG
    img_io = io.BytesIO()
    images[label_index].convert('L').save(img_io, 'PNG')
    img_io.seek(0)
    return Response(img_io.getvalue(), mimetype='image/png')


# ===== 近7天生产进度详情页 =====
@bp.route('/progress_history')
def progress_history_page():
    return render_template('progress_history.html')


@bp.route('/api/progress_history')
def api_progress_history():
    """
    返回今日 + 历史7天的同时段进度快照，用于详情页大图对比。
    X轴时间范围：17:00 ~ 次日 03:00（覆盖完整拣货作业时段）。
    总体：每天的 time_slot → total_picked 曲线。
    分人：每人今日 total_count vs 历史7天日均 total_count。
    效率：总体平均效率、新员工平均效率、临时工平均效率。
    """
    wh_id = _req_warehouse_id()
    conn = database.get_db(wh_id)
    c = conn.cursor()

    now = datetime.now()
    logical_today_dt = now - timedelta(days=1) if now.hour < 8 else now
    logical_date_str = logical_today_dt.strftime("%Y-%m-%d")

    # 工作时间槽：18:00 ~ 次日 01:30（按自然时间排列）
    work_slots = []
    for h in range(18, 24):
        for m in range(0, 60, 10):
            work_slots.append(f"{h:02d}:{m:02d}")
    for h in range(0, 2):
        for m in range(0, 60, 10):
            if h == 1 and m > 20:
                break
            work_slots.append(f"{h:02d}:{m:02d}")

    # 1. 今日全天快照
    c.execute('''SELECT time_slot, total_picked FROM picking_progress_snapshot
                 WHERE logical_date = ? ORDER BY time_slot''', (logical_date_str,))
    today_raw = {row["time_slot"]: row["total_picked"] for row in c.fetchall()}
    today_curve = {s: today_raw[s] for s in work_slots if s in today_raw}

    # 2. 历史7天的全天快照
    hist_dates = [(logical_today_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 8)]
    placeholders = ','.join(['?'] * 7)
    c.execute(f'''SELECT logical_date, time_slot, total_picked FROM picking_progress_snapshot
                  WHERE logical_date IN ({placeholders}) ORDER BY logical_date, time_slot''',
              hist_dates)
    hist_curves_raw = {}  # date -> {time_slot: total_picked}
    for row in c.fetchall():
        d = row["logical_date"]
        if d not in hist_curves_raw:
            hist_curves_raw[d] = {}
        hist_curves_raw[d][row["time_slot"]] = row["total_picked"]

    # 只保留工作时段内的数据
    hist_curves = {}
    for d, slots_map in hist_curves_raw.items():
        hist_curves[d] = {s: slots_map[s] for s in work_slots if s in slots_map}

    # 3. 计算历史7天均值曲线（只算工作时段）
    avg_curve = {}
    for slot in work_slots:
        vals = [hist_curves[d].get(slot, 0) for d in hist_dates if d in hist_curves and hist_curves[d].get(slot, 0) > 0]
        if vals:
            avg_curve[slot] = round(sum(vals) / len(vals))

    # 4. 分人对比：今日实时 vs 历史7天日均件数
    c.execute('''SELECT name, team_name, is_new_staff, is_temp_worker, total_count,
                        feeding_area_count, seasoning_area_count, efficiency
                 FROM worker_realtime ORDER BY total_count DESC''')
    today_workers = [dict(row) for row in c.fetchall()]

    # 优先用 daily_worker_picking_summary（件数），否则回退到单量
    c.execute(f'''SELECT name, summary_date, total_count
                  FROM daily_worker_picking_summary
                  WHERE summary_date IN ({placeholders})''', hist_dates)
    summary_rows = c.fetchall()

    if summary_rows:
        # 有件数历史数据
        worker_hist = {}  # name -> [day_totals]
        for row in summary_rows:
            name = row["name"]
            if name not in worker_hist:
                worker_hist[name] = []
            worker_hist[name].append(row["total_count"])
        hist_unit = "件"
    else:
        # 回退：用单量数据（标注清楚）
        c.execute(f'''SELECT name, work_date, SUM(bill_count) AS day_total
                      FROM worker_warehouse_wave_bill_count
                      WHERE work_date IN ({placeholders})
                      GROUP BY name, work_date''', hist_dates)
        worker_hist = {}
        for row in c.fetchall():
            name = row["name"]
            if name not in worker_hist:
                worker_hist[name] = []
            worker_hist[name].append(row["day_total"])
        hist_unit = "单"

    # 当 hist_unit 为"单"时，也查今日单量用于同单位对比
    today_bill_map = {}
    if hist_unit == "单":
        today_str = logical_today_dt.strftime("%Y-%m-%d")
        c.execute('''SELECT name, SUM(bill_count) AS total FROM worker_warehouse_wave_bill_count
                     WHERE work_date = ? GROUP BY name''', (today_str,))
        for row in c.fetchall():
            today_bill_map[row["name"]] = row["total"]

    # 无论 hist_unit 是什么，都查近7天日均件数（来自 daily_worker_picking_summary）
    worker_hist_count = {}  # name -> [day_totals] 件数
    c.execute(f'''SELECT name, summary_date, total_count
                  FROM daily_worker_picking_summary
                  WHERE summary_date IN ({placeholders})''', hist_dates)
    for row in c.fetchall():
        name = row["name"]
        if name not in worker_hist_count:
            worker_hist_count[name] = []
        worker_hist_count[name].append(row["total_count"])

    workers_compare = []
    for w in today_workers:
        name = w["name"]
        today_count = w["total_count"] or 0
        hist_vals = worker_hist.get(name, [])
        hist_avg = round(sum(hist_vals) / len(hist_vals), 1) if hist_vals else None
        # 近7天日均件数
        hist_count_vals = worker_hist_count.get(name, [])
        hist_avg_count = round(sum(hist_count_vals) / len(hist_count_vals), 1) if hist_count_vals else None
        # 计算对比百分比：件数对件数，单量对单量
        if hist_unit == "件":
            compare_today = today_count
        else:
            compare_today = today_bill_map.get(name, 0)
        diff_pct = round((compare_today - hist_avg) / hist_avg * 100, 1) if (hist_avg and hist_avg > 0 and compare_today > 0) else None
        workers_compare.append({
            "name": name,
            "team_name": w.get("team_name", "") or "",
            "is_new_staff": bool(w.get("is_new_staff")),
            "is_temp_worker": bool(w.get("is_temp_worker")),
            "today_count": today_count,
            "today_bills": today_bill_map.get(name) if hist_unit == "单" else None,
            "hist_avg_count": hist_avg_count,
            "hist_avg_final": hist_avg,
            "hist_days": len(hist_vals),
            "diff_pct": diff_pct,
        })

    # 5. 效率统计
    active_workers = [w for w in today_workers if (w.get("total_count") or 0) > 0]
    new_workers = [w for w in active_workers if w.get("is_new_staff")]
    temp_workers = [w for w in active_workers if w.get("is_temp_worker")]
    regular_workers = [w for w in active_workers if not w.get("is_new_staff") and not w.get("is_temp_worker")]

    def calc_avg_efficiency(worker_list):
        effs = [w["efficiency"] for w in worker_list if w.get("efficiency") and w["efficiency"] > 0]
        return round(sum(effs) / len(effs), 1) if effs else None

    efficiency_stats = {
        "overall_avg": calc_avg_efficiency(active_workers),
        "overall_count": len(active_workers),
        "new_staff_avg": calc_avg_efficiency(new_workers),
        "new_staff_count": len(new_workers),
        "temp_worker_avg": calc_avg_efficiency(temp_workers),
        "temp_worker_count": len(temp_workers),
        "regular_avg": calc_avg_efficiency(regular_workers),
        "regular_count": len(regular_workers),
    }

    # 6. 昨日对比数据（单独高亮）
    yesterday_str = hist_dates[0]  # hist_dates 第一个即昨天（距今1天）
    yesterday_curve = hist_curves.get(yesterday_str, {})
    # 昨日同时段对比值
    yesterday_at_now = None
    for i in range(len(work_slots) - 1, -1, -1):
        if work_slots[i] in today_curve:
            yesterday_at_now = yesterday_curve.get(work_slots[i])
            break
    # 昨日最终完成量（工作时段内最大值）
    yesterday_final = max(yesterday_curve.values()) if yesterday_curve else None

    # 7. 销售预测（今晚拣货目标量）
    c.execute('SELECT forecast_qty, fetched_at FROM sales_forecast WHERE logical_date = ?',
              (logical_date_str,))
    forecast_row = c.fetchone()
    forecast_data = None
    if forecast_row:
        forecast_data = {
            "target_qty": forecast_row["forecast_qty"],
            "fetched_at": forecast_row["fetched_at"],
            "logical_date": logical_date_str,
        }

    # 8. 按波次拆分的今日拣货曲线（凌晨达/上午达/下午达）
    c.execute('''SELECT COUNT(*) AS cnt, MIN(time_slot) AS min_slot FROM picking_progress_snapshot_wave
                 WHERE logical_date = ?''', (logical_date_str,))
    wave_snapshot_row = c.fetchone()
    wave_snapshot_count = wave_snapshot_row["cnt"]
    wave_min_slot = wave_snapshot_row["min_slot"]

    # 判断曲线是否存在"开头缺口"：最早快照时段明显晚于工作时段起点(work_slots[0])，
    # 说明实时采集在中途才开始记录，前面一段是空的，需要用接口数据强制补齐
    need_backfill = False
    if wave_snapshot_count == 0:
        need_backfill = True
    elif wave_min_slot and work_slots:
        try:
            min_idx = work_slots.index(wave_min_slot) if wave_min_slot in work_slots else 0
        except ValueError:
            min_idx = 0
        # 距离工作时段起点超过 20 分钟（2个10分钟槽位）即视为存在缺口
        if min_idx > 2:
            need_backfill = True

    # 自动调用储位拣货子任务 API 抓取回填，保证曲线从工作时段起点开始完整、文字报告可用
    if need_backfill:
        try:
            from _backfill_snapshots import backfill_wave_for_date
            written = backfill_wave_for_date(logical_date_str, force=True)
            if written:
                print(f"[进度详情] 检测到分波次趋势存在缺口，已自动回填(force): {written}")
        except Exception as e:
            print(f"[进度详情] 自动回填分波次快照失败: {e}")

    c.execute('''SELECT time_slot, wave_name, total_picked FROM picking_progress_snapshot_wave
                 WHERE logical_date = ? ORDER BY time_slot''', (logical_date_str,))
    wave_curves_raw = {}  # wave_name -> {time_slot: total_picked}
    for row in c.fetchall():
        wn = row["wave_name"]
        if wn not in wave_curves_raw:
            wave_curves_raw[wn] = {}
        wave_curves_raw[wn][row["time_slot"]] = row["total_picked"]
    wave_curves = {}
    for wn, slots_map in wave_curves_raw.items():
        curve = {s: slots_map[s] for s in work_slots if s in slots_map}
        if any(v > 0 for v in curve.values()):
            wave_curves[wn] = curve

    # 9. 按波次预测分母（用应调拨总量 shouldAllotTotalCount 作为该波次的预测/应完成件量）
    #    数据来源：global_warehouse_realtime_wave，按 fulfillment_wave 汇总各目的仓 volume
    WAVE_ID_TO_NAME = {1: "上午达", 2: "下午达", 5: "凌晨达"}
    c.execute('''SELECT fulfillment_wave, SUM(volume) AS total_volume
                 FROM global_warehouse_realtime_wave GROUP BY fulfillment_wave''')
    wave_forecast_qty = {}
    for row in c.fetchall():
        wn = WAVE_ID_TO_NAME.get(row["fulfillment_wave"])
        if wn:
            wave_forecast_qty[wn] = row["total_volume"] or 0

    # 10. 按小时汇总的文字说明报告数据：累计 + 单小时增量，每个波次独立
    # 找到 work_slots 中"当前实际时刻"对应的索引，只统计已经过去的整点，不外推未来时段
    current_slot_min = (now.minute // 10) * 10
    current_slot_label = f"{now.hour:02d}:{current_slot_min:02d}"
    if current_slot_label in work_slots:
        current_idx = work_slots.index(current_slot_label)
    elif now.hour >= 18:
        # 当前时间早于 work_slots 起始(18:00)，说明还未进入工作时段
        current_idx = -1
    else:
        # 当前时间在 00:00~工作时段结束之后（次日凌晨且已超过 work_slots 覆盖范围），按最后一个槽位处理
        current_idx = len(work_slots) - 1

    hourly_report = {}
    for wn, curve in wave_curves.items():
        # 取每个"已经过去"的整点（HH:00）对应的累计值；若某整点缺失则用小于该整点的最近时段值
        seen_hours = []
        for i, slot in enumerate(work_slots):
            if i > current_idx:
                break  # 未来时段不纳入文字报告
            hh, mm = slot.split(":")
            if mm == "00":
                seen_hours.append(slot)
        # 按曲线出现顺序，找到每个整点时刻对应的累计值（取<=该整点的最后一个已知值）
        # 用 work_slots 索引比较，避免跨零点字符串比较错误
        sorted_slots_with_val = [(s, curve[s]) for s in work_slots if s in curve]
        hour_points = []  # [(hour_label, cumulative)]
        for hh_slot in seen_hours:
            hh_idx = work_slots.index(hh_slot)
            val = None
            for s, v in sorted_slots_with_val:
                if work_slots.index(s) <= hh_idx:
                    val = v
            if val is not None:
                hour_points.append((hh_slot, val))
        if not hour_points:
            continue
        forecast_qty_wave = wave_forecast_qty.get(wn, 0)
        lines = []
        prev_cum = 0
        for hh_slot, cum in hour_points:
            delta = cum - prev_cum
            hour_label = hh_slot.split(":")[0]
            hour_label = str(int(hour_label))  # 去掉前导0，如 "07:00" -> "7"
            hour_pct = round(cum / forecast_qty_wave * 100, 1) if forecast_qty_wave > 0 else None
            lines.append({
                "hour": hour_label,
                "cumulative": cum,
                "delta": delta,
                "progress_pct": hour_pct,
            })
            prev_cum = cum
            # 该波次已达到100%进度，后续整点不再继续更新展示
            if hour_pct is not None and hour_pct >= 100:
                break
        # 最新累计值取曲线中实际存在的最大时段值（可能晚于最后一个整点，如21:40）
        actual_latest_cum = sorted_slots_with_val[-1][1] if sorted_slots_with_val else hour_points[-1][1]
        progress_pct = round(actual_latest_cum / forecast_qty_wave * 100, 1) if forecast_qty_wave > 0 else None
        hourly_report[wn] = {
            "progress_pct": progress_pct,
            "latest_cumulative": actual_latest_cum,
            "forecast_qty": forecast_qty_wave,
            "hours": lines,
        }

    conn.close()

    return jsonify({
        "logical_date": logical_date_str,
        "current_time": now.strftime("%H:%M"),
        "work_slots": work_slots,
        "today_curve": today_curve,
        "hist_dates": [d for d in hist_dates if d in hist_curves],
        "hist_curves": hist_curves,
        "avg_curve": avg_curve,
        "yesterday_date": yesterday_str,
        "yesterday_curve": yesterday_curve,
        "yesterday_at_now": yesterday_at_now,
        "yesterday_final": yesterday_final,
        "workers_compare": workers_compare,
        "hist_unit": hist_unit,
        "efficiency_stats": efficiency_stats,
        "forecast": forecast_data,
        "wave_curves": wave_curves,
        "wave_forecast_qty": wave_forecast_qty,
        "hourly_report": hourly_report,
    })
