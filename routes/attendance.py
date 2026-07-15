# -*- coding: utf-8 -*-
"""
班组出勤统计 - 路由模块
包含：页面渲染、出勤数据查询/刷新、班组配置管理 API
"""
from flask import Blueprint, render_template, jsonify, request
from datetime import datetime
import sqlite3
import database
from spiders.base import WAREHOUSES, DEFAULT_WAREHOUSE_ID

bp = Blueprint('attendance', __name__)


def _req_warehouse_id():
    """从请求参数中获取仓库ID（客户端级别，不影响全局状态）"""
    wh_id = request.args.get('warehouseId', '').strip()
    if wh_id and wh_id in WAREHOUSES:
        return wh_id
    return DEFAULT_WAREHOUSE_ID


# ======================================================================
#  页面
# ======================================================================

@bp.route('/attendance')
def attendance_page():
    return render_template('attendance.html')


# ======================================================================
#  出勤数据 API
# ======================================================================

@bp.route('/api/attendance')
def attendance_api():
    """
    查询班组出勤数据。
    统一从 attendance_gantt_detail 出发，用当前班组映射实时计算矩阵和合计，
    确保班组名称永远一致。

    参数:  date: 'YYYY-MM-DD'，默认今天
    返回:  { date, hours, teams, matrix, hourly_total, total_people, team_people, has_data }
    """
    from spiders.attendance import AttendanceService

    wh_id = _req_warehouse_id()
    target_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    current_team_map = AttendanceService.get_team_map()

    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('''
        SELECT name, team_name, start_time
        FROM attendance_gantt_detail
        WHERE attendance_date = ?
    ''', (target_date,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        return jsonify({
            "date": target_date,
            "hours": [],
            "teams": [],
            "matrix": {},
            "hourly_total": {},
            "has_data": False
        })

    # 用当前班组映射重新计算所有数据
    # 矩阵: 每人按 start_time 整点归入一次（和原 compute_team_hourly_attendance 逻辑一致）
    from collections import defaultdict
    # { hour: { team: set(names) } }  — 按整点+班组去重
    hour_team_names = defaultdict(lambda: defaultdict(set))
    # 按班组去重人头
    team_name_set = defaultdict(set)

    for row in rows:
        name = row['name']
        team = current_team_map.get(name, '未分组')
        start_str = row['start_time']  # 'HH:MM'
        try:
            hour = int(start_str.split(':')[0])
        except (ValueError, IndexError):
            continue

        hour_team_names[hour][team].add(name)
        team_name_set[team].add(name)

    # 构建 matrix: { team: { hour_str: count } }
    matrix = {}
    teams_set = set()
    hours_set = set()
    hourly_total = {}

    for hour, teams in hour_team_names.items():
        hours_set.add(hour)
        for team, names in teams.items():
            teams_set.add(team)
            count = len(names)
            if team not in matrix:
                matrix[team] = {}
            matrix[team][str(hour)] = count
            hourly_total[str(hour)] = hourly_total.get(str(hour), 0) + count

    sorted_hours = sorted(hours_set)

    # 班组按总出勤人数降序
    team_totals = {t: sum(matrix.get(t, {}).values()) for t in teams_set}
    sorted_teams = sorted(teams_set, key=lambda t: -team_totals.get(t, 0))

    # 各班组去重人数 + 总去重人数
    team_people = {t: len(names) for t, names in team_name_set.items()}
    all_people = set()
    for names in team_name_set.values():
        all_people.update(names)
    total_people = len(all_people)

    return jsonify({
        "date": target_date,
        "hours": sorted_hours,
        "teams": sorted_teams,
        "matrix": matrix,
        "hourly_total": hourly_total,
        "total_people": total_people,
        "team_people": team_people,
        "has_data": True
    })


@bp.route('/api/attendance/refresh', methods=['POST'])
def attendance_refresh():
    """手动触发考勤数据刷新。"""
    from spiders.attendance import AttendanceService

    target_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))
    try:
        hourly_data = AttendanceService.refresh(target_date)
        if hourly_data:
            total_teams = set()
            total_hours = set()
            for date_str, hours in hourly_data.items():
                for hour, teams in hours.items():
                    total_hours.add(hour)
                    total_teams.update(teams.keys())
            return jsonify({
                "ok": True,
                "msg": f"刷新成功：{len(total_teams)} 个班组, {len(total_hours)} 个时段"
            })
        else:
            return jsonify({"ok": False, "msg": "未获取到考勤数据"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"刷新失败: {str(e)}"})


@bp.route('/api/attendance/history')
def attendance_history():
    """查询有数据的历史日期列表。"""
    wh_id = _req_warehouse_id()
    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('''
        SELECT DISTINCT attendance_date
        FROM team_attendance_hourly
        ORDER BY attendance_date DESC
        LIMIT 30
    ''')
    dates = [row['attendance_date'] for row in c.fetchall()]
    conn.close()
    return jsonify({"dates": dates})


@bp.route('/api/attendance/gantt')
def attendance_gantt():
    """
    甘特图数据 API（从数据库读取已保存的明细）。
    参数:  date: 'YYYY-MM-DD'，默认今天
    返回:  {
        date, has_data,
        teams: [ { team_name, member_count, members: [ {name, start, end}, ... ] } ],
        time_range: { min, max }
    }
    """
    from spiders.attendance import AttendanceService

    wh_id = _req_warehouse_id()
    target_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))

    # 从数据库读取已保存的甘特图明细
    day_data = AttendanceService.load_gantt_data(target_date)

    empty = {
        "date": target_date,
        "has_data": False,
        "teams": [],
        "time_range": {"min": "06:00", "max": "23:00"}
    }

    if not day_data:
        return jsonify(empty)

    # 读取该日各班组整点出勤数据（用于折叠态汇总）
    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('''
        SELECT hour_slot, team_name, head_count
        FROM team_attendance_hourly
        WHERE attendance_date = ?
    ''', (target_date,))
    hourly_rows = c.fetchall()
    conn.close()

    # { team_name: { hour_slot: head_count } }
    team_hourly_map = {}
    for row in hourly_rows:
        tn = row['team_name']
        if tn not in team_hourly_map:
            team_hourly_map[tn] = {}
        team_hourly_map[tn][row['hour_slot']] = row['head_count']

    # 计算时间范围
    all_starts = []
    all_ends = []
    teams_list = []

    # 按班组人数降序排列
    sorted_teams = sorted(day_data.items(), key=lambda x: -len(x[1]))
    for team_name, members in sorted_teams:
        teams_list.append({
            "team_name": team_name,
            "member_count": len(members),
            "members": members,
            "hourly_summary": team_hourly_map.get(team_name, {}),
        })
        for m in members:
            all_starts.append(m['start'])
            all_ends.append(m['end'])

    time_min = min(all_starts) if all_starts else "06:00"
    time_max = max(all_ends) if all_ends else "23:00"

    return jsonify({
        "date": target_date,
        "has_data": True,
        "teams": teams_list,
        "time_range": {"min": time_min, "max": time_max},
    })


# ======================================================================
#  导出出勤明细 API
# ======================================================================

@bp.route('/api/attendance/export')
def attendance_export():
    """
    导出当日出勤明细数据（JSON）。
    参数:  date: 'YYYY-MM-DD'，默认今天
    返回:  {
        date, items: [
            { name, team, is_temp, start, end, duration },
            ...
        ]
    }
    出勤时长 = 下班时间 - 上班时间（支持跨天计算，格式 'Xh Ym'）
    """
    wh_id = _req_warehouse_id()
    target_date = request.args.get('date', datetime.now().strftime("%Y-%m-%d"))

    conn = database.get_db(wh_id)
    c = conn.cursor()
    c.execute('''
        SELECT g.name, g.team_name, g.start_time, g.end_time,
               COALESCE(w.is_temp_worker, 0) AS is_temp
        FROM attendance_gantt_detail g
        LEFT JOIN worker_info_cache w ON g.name = w.name
        WHERE g.attendance_date = ?
        ORDER BY g.team_name, g.start_time
    ''', (target_date,))
    rows = c.fetchall()
    conn.close()

    items = []
    for row in rows:
        start_str = row['start_time']
        end_str = row['end_time']

        # 计算出勤时长（支持跨天：如果 end < start 则跨天 +24h）
        try:
            sp = start_str.split(':')
            ep = end_str.split(':')
            start_min = int(sp[0]) * 60 + int(sp[1])
            end_min = int(ep[0]) * 60 + int(ep[1])
            if end_min < start_min:
                end_min += 24 * 60  # 跨天
            diff = end_min - start_min
            hours = diff // 60
            mins = diff % 60
            duration = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
        except Exception:
            duration = '-'

        items.append({
            "name": row['name'],
            "team": row['team_name'],
            "is_temp": bool(row['is_temp']),
            "start": start_str,
            "end": end_str,
            "duration": duration,
        })

    return jsonify({
        "date": target_date,
        "items": items,
    })


# ======================================================================
#  班组配置管理 API
# ======================================================================

@bp.route('/api/attendance/team-config', methods=['GET'])
def team_config_list():
    """
    获取自定义班组配置列表。
    可选参数:  team_name — 按班组筛选
    返回:  { items: [...], teams: [...] }
    """
    from spiders.attendance import AttendanceService

    filter_team = request.args.get('team_name', '').strip()
    items = AttendanceService.list_team_config()
    if filter_team:
        items = [i for i in items if i['team_name'] == filter_team]

    teams = AttendanceService.get_all_team_names()

    return jsonify({"items": items, "teams": teams})


@bp.route('/api/attendance/team-config', methods=['POST'])
def team_config_set():
    """
    设置/更新人员的自定义班组。
    Body JSON: { "name": "...", "team_name": "..." }
        或批量: { "members": [ { "name": "...", "team_name": "..." }, ... ] }
    """
    from spiders.attendance import AttendanceService

    data = request.get_json(silent=True) or {}

    try:
        # 批量模式
        members = data.get('members')
        if members and isinstance(members, list):
            valid = [m for m in members if m.get('name') and m.get('team_name')]
            if not valid:
                return jsonify({"ok": False, "msg": "无有效的配置数据"}), 400
            AttendanceService.batch_set_team_config(valid)
            return jsonify({"ok": True, "msg": f"已更新 {len(valid)} 人的班组配置"})

        # 单条模式
        name = data.get('name', '').strip()
        team_name = data.get('team_name', '').strip()
        if not name or not team_name:
            return jsonify({"ok": False, "msg": "name 和 team_name 不能为空"}), 400

        AttendanceService.set_team_config(name, team_name)
        return jsonify({"ok": True, "msg": f"已设置 {name} → {team_name}"})
    except sqlite3.OperationalError as e:
        print(f"[ERROR] 保存班组配置失败: {e}")
        return jsonify({"ok": False, "msg": f"数据库繁忙，请稍后重试: {e}"}), 503


@bp.route('/api/attendance/team-config', methods=['DELETE'])
def team_config_delete():
    """
    删除人员的自定义班组配置。
    参数: name=xxx
    """
    from spiders.attendance import AttendanceService

    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({"ok": False, "msg": "name 不能为空"}), 400

    try:
        AttendanceService.delete_team_config(name)
        return jsonify({"ok": True, "msg": f"已删除 {name} 的自定义班组配置"})
    except sqlite3.OperationalError as e:
        print(f"[ERROR] 删除班组配置失败: {e}")
        return jsonify({"ok": False, "msg": f"数据库繁忙，请稍后重试: {e}"}), 503


@bp.route('/api/attendance/team-config/reset-all', methods=['POST'])
def team_config_reset_all():
    """清除所有自定义班组配置，恢复全员为系统原班组。"""
    from spiders.attendance import AttendanceService
    try:
        count = AttendanceService.clear_all_team_config()
        return jsonify({"ok": True, "msg": f"已清除 {count} 条自定义配置，全员已恢复为系统原班组"})
    except sqlite3.OperationalError as e:
        return jsonify({"ok": False, "msg": f"数据库繁忙，请稍后重试: {e}"}), 503


@bp.route('/api/attendance/team-config/teams', methods=['GET'])
def team_config_teams():
    """获取所有可用班组名称列表。"""
    from spiders.attendance import AttendanceService
    teams = AttendanceService.get_all_team_names()
    return jsonify({"teams": teams})


@bp.route('/api/attendance/sync-system-teams', methods=['POST'])
def sync_system_teams():
    """
    从 KLWMS HRM 接口拉取 428 仓全量在职人员的系统班组，
    更新 worker_info_cache 中的 team_name（原班组）。
    返回更新统计：总人数、更新人数、新增人数、各班组人数。
    """
    from spiders.base import get_shared_session, request_with_retry
    import time as _time

    wh_id = _req_warehouse_id()
    try:
        session = get_shared_session()
        params = {
            "warehouseValidity": "EFFECTIVE",
            "warehouseIdList": wh_id,
            "jobStatus": "INCUMBENCY",
            "pageNo": 1,
            "pageSize": 1000,
        }
        res = request_with_retry(session, "/hrm/labour/inhouse/user/r/pageUserList", params)
        if not res or not isinstance(res.get("data"), dict):
            return jsonify({"ok": False, "msg": "HRM 接口请求失败"}), 502

        users = res["data"].get("pageContent", [])
        if not users:
            return jsonify({"ok": False, "msg": "HRM 接口返回空数据"}), 502

        # 构建 name → system_team 映射
        system_map = {}
        for u in users:
            name = u.get("name", "").strip()
            team = u.get("teamOrgName", "").strip()
            if name and team:
                system_map[name] = team

        # 更新 worker_info_cache
        conn = database.get_db(wh_id)
        c = conn.cursor()
        c.execute("SELECT name, team_name FROM worker_info_cache")
        cached = {row["name"]: row["team_name"] for row in c.fetchall()}

        updated = 0
        added = 0
        for name, sys_team in system_map.items():
            if name in cached:
                if cached[name] != sys_team:
                    c.execute("UPDATE worker_info_cache SET team_name = ? WHERE name = ?",
                              (sys_team, name))
                    updated += 1
            else:
                c.execute("""INSERT INTO worker_info_cache
                             (name, team_name, job_type, is_new_staff, is_temp_worker)
                             VALUES (?, ?, '', 0, 0)""",
                          (name, sys_team))
                added += 1

        conn.commit()
        conn.close()

        # 统计各班组人数
        team_counts = {}
        for team in system_map.values():
            team_counts[team] = team_counts.get(team, 0) + 1

        return jsonify({
            "ok": True,
            "msg": f"同步完成：系统 {len(system_map)} 人，更新 {updated} 人，新增 {added} 人",
            "system_total": len(system_map),
            "updated": updated,
            "added": added,
            "team_counts": dict(sorted(team_counts.items())),
        })
    except Exception as e:
        print(f"[ERROR] 同步系统班组失败: {e}")
        return jsonify({"ok": False, "msg": f"同步失败: {e}"}), 500


@bp.route('/api/attendance/team-members', methods=['GET'])
def team_members_list():
    """
    获取所有人员的班组全貌（原班组 + 自定义班组 + 生效班组）。
    可选参数:
      search — 按姓名模糊搜索
      team   — 按生效班组筛选
      custom_only — 'true' 时只显示有自定义配置的人员
    返回: { items: [...], teams: [...], total: N }
    """
    from spiders.attendance import AttendanceService

    search = request.args.get('search', '').strip()
    filter_orig = request.args.get('orig_team', '').strip()
    filter_team = request.args.get('team', '').strip()
    custom_only = request.args.get('custom_only', '').lower() == 'true'

    all_members = AttendanceService.get_all_members_with_team()

    # 从全员数据直接提取生效班组和原班组列表（避免重复查询）
    teams = sorted(set(m['effective_team'] for m in all_members if m['effective_team'] and m['effective_team'] != '未分组'))
    orig_teams = sorted(set(m['original_team'] for m in all_members if m['original_team']))

    # 筛选
    items = all_members
    if search:
        items = [m for m in items if search in m['name']]
    if filter_orig:
        items = [m for m in items if m['original_team'] == filter_orig]
    if filter_team:
        items = [m for m in items if m['effective_team'] == filter_team]
    if custom_only:
        items = [m for m in items if m['is_custom']]

    return jsonify({
        "items": items,
        "teams": teams,
        "orig_teams": orig_teams,
        "total": len(all_members),
        "filtered": len(items),
    })
