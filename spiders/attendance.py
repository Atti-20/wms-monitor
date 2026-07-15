# -*- coding: utf-8 -*-
"""
考勤数据抓取 - 按班组+整点统计出勤人数（OOP 重构版）

API: /hrm/attendance/r/query
返回结构:
  data.pageContent[]: { labourUserName, attenceDayVoList[]: { attenceDate, attenceInfoVoList[]: { firstClockinTime, endClockinTime } } }
  data.page: { currentPageNo, pageSize, totalCount, totalPageCount }

班组映射优先级:
  1. team_member_config 表（手动配置） > 2. worker_info_cache 表 > 3. 标记为 '未分组'
"""
from datetime import datetime
from collections import defaultdict

import database
from spiders.base import get_shared_session, request_with_retry, get_warehouse_id


class AttendanceService:
    """班组出勤统计服务：抓取考勤、计算班组-时段出勤、管理自定义班组映射。"""

    PAGE_SIZE = 200

    # ------------------------------------------------------------------
    #  班组映射（自定义配置 CRUD）
    # ------------------------------------------------------------------

    @staticmethod
    def get_team_map():
        """
        获取人员→班组映射，优先 team_member_config（手动配置），
        缺失时再取 worker_info_cache。
        返回: { name: team_name }
        """
        conn = database.get_db()
        c = conn.cursor()

        # 先加载 worker_info_cache 作为底层默认
        c.execute("SELECT name, team_name FROM worker_info_cache WHERE team_name IS NOT NULL")
        team_map = {row['name']: row['team_name'] for row in c.fetchall()}

        # 再用 team_member_config 覆盖（自定义优先级更高）
        c.execute("SELECT name, team_name FROM team_member_config")
        for row in c.fetchall():
            team_map[row['name']] = row['team_name']

        conn.close()
        return team_map

    @staticmethod
    def list_team_config():
        """列出所有自定义班组配置记录。"""
        conn = database.get_db()
        c = conn.cursor()
        c.execute("SELECT name, team_name, updated_at FROM team_member_config ORDER BY team_name, name")
        rows = [dict(row) for row in c.fetchall()]
        conn.close()
        return rows

    @staticmethod
    def set_team_config(name, team_name):
        """设置/更新一个人的自定义班组（带 database is locked 重试）。"""
        def _do(conn, c):
            c.execute('''
                INSERT OR REPLACE INTO team_member_config (name, team_name, updated_at)
                VALUES (?, ?, datetime('now', 'localtime'))
            ''', (name, team_name))
        database.execute_with_retry(_do)

    @staticmethod
    def batch_set_team_config(members):
        """
        批量设置自定义班组（带 database is locked 重试）。
        members: [ { "name": "...", "team_name": "..." }, ... ]
        """
        def _do(conn, c):
            for m in members:
                c.execute('''
                    INSERT OR REPLACE INTO team_member_config (name, team_name, updated_at)
                    VALUES (?, ?, datetime('now', 'localtime'))
                ''', (m['name'], m['team_name']))
        database.execute_with_retry(_do)

    @staticmethod
    def delete_team_config(name):
        """删除一个人的自定义班组配置（带 database is locked 重试）。"""
        def _do(conn, c):
            c.execute("DELETE FROM team_member_config WHERE name = ?", (name,))
        database.execute_with_retry(_do)

    @staticmethod
    def clear_all_team_config():
        """清除所有自定义班组配置，恢复全员为系统原班组。返回被清除的记录数。"""
        result = {"count": 0}
        def _do(conn, c):
            c.execute("SELECT COUNT(*) FROM team_member_config")
            result["count"] = c.fetchone()[0]
            c.execute("DELETE FROM team_member_config")
        database.execute_with_retry(_do)
        return result["count"]

    @staticmethod
    def get_all_team_names():
        """获取当前生效的班组名称列表（从全员的 effective_team 去重，精简冗余）。"""
        members = AttendanceService.get_all_members_with_team()
        teams = set(m['effective_team'] for m in members if m['effective_team'] and m['effective_team'] != '未分组')
        return sorted(teams)

    @staticmethod
    def get_all_members_with_team():
        """
        获取所有人员的班组信息全貌，用于前端展示和编辑。
        数据来源合并: worker_info_cache + team_member_config + attendance_gantt_detail
        （确保考勤中出现但档案缺失的人也能显示）
        返回: [
            { name, original_team, custom_team, effective_team, is_custom },
            ...
        ]
        """
        conn = database.get_db()
        c = conn.cursor()

        # 1) 原始班组（员工档案）+ 临时工标记
        c.execute("SELECT name, team_name, is_temp_worker FROM worker_info_cache")
        original_map = {row['name']: row['team_name'] for row in c.fetchall()}
        c.execute("SELECT name FROM worker_info_cache WHERE is_temp_worker = 1")
        temp_set = {row['name'] for row in c.fetchall()}

        # 2) 自定义班组
        c.execute("SELECT name, team_name FROM team_member_config")
        custom_map = {row['name']: row['team_name'] for row in c.fetchall()}

        # 3) 考勤中出现的人员（含未分组的人），取最近一天的记录
        c.execute("SELECT DISTINCT name, team_name FROM attendance_gantt_detail WHERE attendance_date = (SELECT MAX(attendance_date) FROM attendance_gantt_detail)")
        attendance_map = {row['name']: row['team_name'] for row in c.fetchall()}

        conn.close()

        # 4) 合并所有来源的人员
        all_names = set(original_map.keys()) | set(custom_map.keys()) | set(attendance_map.keys())
        result = []
        for name in sorted(all_names):
            orig = original_map.get(name) or attendance_map.get(name, '')
            cust = custom_map.get(name)
            result.append({
                "name": name,
                "original_team": orig or '',
                "custom_team": cust or '',
                "effective_team": cust or orig or '未分组',
                "is_custom": name in custom_map,
                "is_temp": name in temp_set,
            })
        return result

    # ------------------------------------------------------------------
    #  考勤数据抓取
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_hhmm(time_str):
        """解析 'HH:MM' 格式，返回 (hour, minute) 元组，无效返回 None。"""
        if not time_str or not time_str.strip():
            return None
        try:
            parts = time_str.strip().split(':')
            return (int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return None

    @classmethod
    def fetch_attendance_for_month(cls, year_month):
        """
        抓取指定月份的全量考勤数据（自动分页）。
        year_month: 'YYYY-MM' 格式
        返回: [ { labourUserName, attenceDayVoList: [...] }, ... ]
        """
        session = get_shared_session()
        all_records = []
        page_no = 1

        while True:
            params = {
                "warehouseValidity": "EFFECTIVE",
                "warehouseId": get_warehouse_id(),
                "labourUserName": "",
                "labourUserMobile": "",
                "attenceDate": year_month,
                "pageNo": page_no,
                "pageSize": cls.PAGE_SIZE,
            }

            result = request_with_retry(session, "/hrm/attendance/r/query", params)
            if not result or result.get('code') != 200:
                print(f"[考勤] 第 {page_no} 页请求失败: {result}")
                break

            data = result.get('data', {})
            page_content = data.get('pageContent', [])
            all_records.extend(page_content)

            page_info = data.get('page', {})
            total_pages = page_info.get('totalPageCount', 1)
            print(f"[考勤] 第 {page_no}/{total_pages} 页, 本页 {len(page_content)} 条")

            if page_no >= total_pages:
                break
            page_no += 1

        print(f"[考勤] 共获取 {len(all_records)} 人的考勤记录")
        return all_records

    # ------------------------------------------------------------------
    #  统计计算
    # ------------------------------------------------------------------

    @classmethod
    def compute_team_hourly_attendance(cls, attendance_records, target_date=None):
        """
        根据考勤记录 + 班组映射，统计每个整点新到岗的人数。

        班组映射优先级: team_member_config > worker_info_cache > '未分组'

        逻辑：每人只按 firstClockinTime 的整点小时归入一次，
        即统计「该时段新打卡到岗」的人数，而非该时段所有在岗人数。

        target_date: 'YYYY-MM-DD' 格式，如不指定则统计当天
        返回: { 'YYYY-MM-DD': { hour: { team_name: count } } }
        """
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        team_map = cls.get_team_map()

        # 结构: { date: { hour: { team: set(names) } } }
        result = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))

        for record in attendance_records:
            name = record.get('labourUserName', '')
            team = team_map.get(name, '未分组')

            day_list = record.get('attenceDayVoList', []) or []
            for day_entry in day_list:
                att_date = day_entry.get('attenceDate', '')
                if target_date and att_date != target_date:
                    continue

                info_list = day_entry.get('attenceInfoVoList') or []
                if not info_list:
                    continue

                for info in info_list:
                    clockin = cls._parse_hhmm(info.get('firstClockinTime', ''))
                    if not clockin:
                        continue

                    # 只按打卡整点小时归入，每人只计一次
                    arrival_hour = clockin[0]
                    result[att_date][arrival_hour][team].add(name)

        # 转为计数
        final = {}
        for date_str, hours in result.items():
            final[date_str] = {}
            for hour, teams in hours.items():
                final[date_str][hour] = {team: len(names) for team, names in teams.items()}

        return final

    @classmethod
    def compute_gantt_data(cls, attendance_records, target_date=None):
        """
        提取甘特图数据：每人的上班-下班时间段 + 班组信息。
        用于前端绘制班组甘特图（Y轴班组，X轴时间，色块表示每人在岗段）。

        返回: {
            'YYYY-MM-DD': {
                team_name: [
                    { name, start: 'HH:MM', end: 'HH:MM' },
                    ...
                ]
            }
        }
        """
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        team_map = cls.get_team_map()

        # { date: { team: [ {name, start, end}, ... ] } }
        result = defaultdict(lambda: defaultdict(list))

        for record in attendance_records:
            name = record.get('labourUserName', '')
            team = team_map.get(name, '未分组')

            day_list = record.get('attenceDayVoList', []) or []
            for day_entry in day_list:
                att_date = day_entry.get('attenceDate', '')
                if target_date and att_date != target_date:
                    continue

                info_list = day_entry.get('attenceInfoVoList') or []
                if not info_list:
                    continue

                for info in info_list:
                    clockin = cls._parse_hhmm(info.get('firstClockinTime', ''))
                    if not clockin:
                        continue
                    clockout = cls._parse_hhmm(info.get('endClockinTime', ''))

                    start_str = f"{clockin[0]:02d}:{clockin[1]:02d}"
                    if clockout:
                        end_str = f"{clockout[0]:02d}:{clockout[1]:02d}"
                    else:
                        # 尚未下班
                        now = datetime.now()
                        if att_date == now.strftime("%Y-%m-%d"):
                            end_str = f"{now.hour:02d}:{now.minute:02d}"
                        else:
                            end_str = start_str  # 历史无下班记录则不画

                    result[att_date][team].append({
                        "name": name,
                        "start": start_str,
                        "end": end_str,
                    })

        # 排序：每个班组内按打卡时间排
        final = {}
        for date_str, teams in result.items():
            final[date_str] = {}
            for team, members in teams.items():
                final[date_str][team] = sorted(members, key=lambda m: m['start'])

        return final

    # ------------------------------------------------------------------
    #  持久化
    # ------------------------------------------------------------------

    @staticmethod
    def save_team_hourly_attendance(hourly_data):
        """
        将按班组+整点的出勤统计写入数据库。
        hourly_data: { 'YYYY-MM-DD': { hour: { team_name: count } } }
        """
        conn = database.get_db()
        c = conn.cursor()

        for date_str, hours in hourly_data.items():
            for hour, teams in hours.items():
                for team_name, count in teams.items():
                    c.execute('''
                        INSERT OR REPLACE INTO team_attendance_hourly
                        (attendance_date, hour_slot, team_name, head_count)
                        VALUES (?, ?, ?, ?)
                    ''', (date_str, hour, team_name, count))

        conn.commit()
        conn.close()
        total_rows = sum(len(teams) for hours in hourly_data.values() for teams in hours.values())
        print(f"[考勤] 已保存 {total_rows} 条班组时段出勤记录")

    @staticmethod
    def save_gantt_data(gantt_data):
        """
        将甘特图明细写入数据库。
        gantt_data: { 'YYYY-MM-DD': { team_name: [ {name, start, end}, ... ] } }
        """
        conn = database.get_db()
        c = conn.cursor()
        total = 0

        for date_str, teams in gantt_data.items():
            for team_name, members in teams.items():
                for m in members:
                    c.execute('''
                        INSERT OR REPLACE INTO attendance_gantt_detail
                        (attendance_date, name, team_name, start_time, end_time)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (date_str, m['name'], team_name, m['start'], m['end']))
                    total += 1

        conn.commit()
        conn.close()
        print(f"[考勤] 已保存 {total} 条甘特图明细记录")

    @staticmethod
    def load_gantt_data(target_date):
        """
        从数据库读取已保存的甘特图明细。
        返回: { team_name: [ {name, start, end}, ... ] } 或 None
        """
        conn = database.get_db()
        c = conn.cursor()
        c.execute('''
            SELECT name, team_name, start_time, end_time
            FROM attendance_gantt_detail
            WHERE attendance_date = ?
            ORDER BY team_name, start_time
        ''', (target_date,))
        rows = c.fetchall()

        # 查临时工名单
        c.execute("SELECT name FROM worker_info_cache WHERE is_temp_worker = 1")
        temp_set = {r['name'] for r in c.fetchall()}
        conn.close()

        if not rows:
            return None

        result = defaultdict(list)
        for row in rows:
            result[row['team_name']].append({
                "name": row['name'],
                "start": row['start_time'],
                "end": row['end_time'],
                "is_temp": row['name'] in temp_set,
            })
        return dict(result)

    # ------------------------------------------------------------------
    #  一键刷新
    # ------------------------------------------------------------------

    @classmethod
    def refresh(cls, target_date=None):
        """
        一键刷新：拉取本月考勤 → 计算班组时段出勤 → 写入数据库。
        target_date: 'YYYY-MM-DD'，默认今天
        """
        if target_date is None:
            target_date = datetime.now().strftime("%Y-%m-%d")

        year_month = target_date[:7]
        print(f"[考勤] 开始刷新 {target_date} 的班组出勤数据...")

        records = cls.fetch_attendance_for_month(year_month)
        if not records:
            print("[考勤] 未获取到考勤数据")
            return {}

        hourly_data = cls.compute_team_hourly_attendance(records, target_date)
        cls.save_team_hourly_attendance(hourly_data)

        # 同时计算并保存甘特图明细
        gantt_data = cls.compute_gantt_data(records, target_date)
        cls.save_gantt_data(gantt_data)

        return hourly_data


# ======================================================================
#  向后兼容：保留函数形式的入口（旧代码如果直接调用不会报错）
# ======================================================================

def refresh_team_attendance(target_date=None):
    """兼容旧调用: spiders.attendance.refresh_team_attendance(date)"""
    return AttendanceService.refresh(target_date)
