# -*- coding: utf-8 -*-
import os
import time
import requests
from urllib.parse import urlencode
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from database import get_db
from token_manager import (
    get_access_token, invalidate_token, TOKEN_STATUS,
    PROXY_URL
)

# ===================== 配置项 =====================
REQUEST_TIMEOUT = 15
NEW_STAFF_DAYS = 7
STAGNANT_MINUTES = 15
WAREHOUSE_ID = "428"
BASE_URL = "https://klwms.meituan.com"


# ===================== 工具函数 =====================
def safe_get_row_value(row, key, default=None):
    """
    安全获取sqlite3.Row对象的值（兼容.get()写法）
    :param row: sqlite3.Row对象
    :param key: 字段名
    :param default: 默认值
    :return: 字段值 | 默认值
    """
    if row is None:
        return default
    try:
        return row[key]
    except KeyError:
        return default


def map_wave_name_to_id(wave_name):
    if not wave_name:
        return None
    if "上午达" in wave_name:
        return 1
    if "下午达" in wave_name:
        return 2
    if "凌晨达" in wave_name:
        return 5
    return None


# ===================== 请求工具（NoCode Proxy 模式） =====================
def request_with_retry(session, api_path, params=None, max_retries=2):
    """
    通过 NoCode Proxy 调用 klwms 接口。
    
    鉴权链路：
      GET https://nocode.sankuai.com/proxy/request
      Headers:
        - Origin-Url: https://klwms.meituan.com/{api_path}?{params}
        - access-token: {SSO Token}
    
    Args:
        session: requests.Session 对象
        api_path: 接口路径，如 "/haina/outbound/zonepick/r/pageList"
                  或完整 URL "https://klwms.meituan.com/..."
        params: 请求参数字典
        max_retries: 最大重试次数
    """
    # 构建原始完整 URL
    if api_path.startswith("http"):
        original_url = api_path
    else:
        original_url = f"{BASE_URL}{api_path}"

    if params:
        query_string = urlencode(params, doseq=True)
        original_url = f"{original_url}?{query_string}"

    for attempt in range(max_retries):
        try:
            # 获取 token
            token = get_access_token()
            if not token:
                print("[ERROR] 无法获取有效 Token, 请检查本机 MOA 登录态")
                TOKEN_STATUS["ok"] = False
                return None

            # 通过 Proxy 发起请求
            headers = {
                "Origin-Url": original_url,
                "access-token": token,
            }

            response = session.get(
                PROXY_URL,
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )

            # 处理 HTTP 401 —— Token 失效
            if response.status_code == 401:
                print("[ERROR] HTTP 401 鉴权失败, Token 已失效, 正在尝试刷新...")
                invalidate_token()
                # 重试一次
                token = get_access_token(force_refresh=True)
                if not token:
                    print("[ERROR] Token 刷新失败")
                    return None
                headers["access-token"] = token
                response = session.get(
                    PROXY_URL,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT
                )
                if response.status_code == 401:
                    print("[ERROR] Token 刷新后仍然 401, 请确认 MOA 登录态")
                    return None

            # 处理 429 限流
            if response.status_code == 429:
                print(f"[WARN] 请求被限流 (429), 等待 1s 后重试...")
                time.sleep(1)
                continue

            response.raise_for_status()
            res_data = response.json()

            # 处理接口 JSON 返回的 code:401
            if res_data.get('code') == 401:
                print("[ERROR] 接口返回 401, Token 已失效, 正在刷新...")
                invalidate_token()
                if attempt < max_retries - 1:
                    continue
                return None

            # proxy 返回 50001 表示域名不在白名单
            if res_data.get('code') == 50001:
                print(f"[ERROR] Proxy 返回 50001: 域名不在白名单")
                return None

            return res_data

        except requests.exceptions.RequestException as e:
            print(f"[ERROR] 请求失败 (第{attempt + 1}次): {e}")
            if attempt == max_retries - 1:
                print("[ERROR] 多次重试后请求仍失败, 本次放弃")
                return None
        except ValueError as e:
            print(f"[ERROR] JSON解析失败: {e}")
            return None
    return None


# ===================== 核心爬虫类 =====================
class WMSDataSpider:
    # 考勤查询限流：每人至少间隔 10 分钟才重新查询
    ATTENDANCE_QUERY_INTERVAL = 600  # 秒

    def __init__(self):
        """初始化：验证 Token 可用性"""
        self.new_staff_days = NEW_STAFF_DAYS
        self.session = requests.Session()
        self._attendance_last_query = {}  # {name: timestamp} 考勤查询限流记录
        # 设置通用请求头（不再需要 Cookie）
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0",
            "Content-Type": "application/json"
        })
        # 尝试获取 Token 验证连通性
        token = get_access_token()
        if token:
            print("[OK] SSO Token 获取成功, 系统就绪")
        else:
            print("[WARN] SSO Token 获取失败, 系统将在后续轮询时自动重试")

    def parse_ms_timestamp(self, ts):
        """转换毫秒级时间戳为datetime对象"""
        if not ts:
            return None
        try:
            return datetime.fromtimestamp(int(ts) / 1000.0)
        except (ValueError, TypeError):
            return None

    def process_and_save(self):
        """核心逻辑：抓取数据、处理缓存、计算人效、写入数据库"""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] [START] 触发智能抓取与缓存融合计算...")

        # 1. 确定逻辑日期
        actual_now = datetime.now()
        logical_today = actual_now - timedelta(days=1) if actual_now.hour < 8 else actual_now
        logical_tomorrow = logical_today + timedelta(days=1)
        current_month = logical_today.strftime("%Y-%m")
        today_date = logical_today.strftime("%Y-%m-%d")
        tomorrow_date = logical_tomorrow.strftime("%Y-%m-%d")

        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            # ===================== 步骤1 + 1.5 并行：拣货数据 & 已生成拣货单 =====================
            api_pick = "/haina/outbound/zonepick/r/pageList"
            base_params = {
                "createTimeStart": f"{today_date} 06:00:00",
                "createTimeEnd": f"{tomorrow_date} 12:00:00",
                "billType": "DB",
                "deliveryRegionIds": "",
                "pageSize": 1000,
                "wareHouseId": WAREHOUSE_ID,
                "warehouseId": WAREHOUSE_ID,
            }

            def _fetch_picking_records():
                """步骤1：抓取拣货数据（全量）"""
                params = {**base_params, "pageNo": 1}
                res = request_with_retry(self.session, api_pick, params)
                if not res:
                    return None
                records = res.get("data", {}).get("pageContent", [])
                print(f"[INFO] 抓取到 {len(records)} 条拣货明细记录")
                return records

            def _fetch_created_records():
                """步骤1.5：抓取已生成的拣货单（分页）"""
                params = {**base_params, "pickStatus": "created", "pageNo": 1}
                all_records = []
                pg = 1
                while True:
                    params["pageNo"] = pg
                    res = request_with_retry(self.session, api_pick, params)
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
                print(f"[INFO] 获取到 {len(all_records)} 条已生成的拣货明细记录")
                return all_records

            # 并行发起两组请求
            with ThreadPoolExecutor(max_workers=2) as executor:
                fut_pick = executor.submit(_fetch_picking_records)
                fut_created = executor.submit(_fetch_created_records)
                picking_records = fut_pick.result()
                all_created_records = fut_created.result()

            if picking_records is None:
                print("[ERROR] 拣货数据抓取失败，本次轮询跳过")
                return
            if all_created_records is None:
                all_created_records = []

            # 统计各仓单量（按区域和履约产品）
            warehouse_bill_wave_counts = {}  # {(warehouse, wave_id): {"seasoning": set(), "feeding": set()}}
            for record in all_created_records:
                warehouse = record.get("bizBillName")
                area = record.get("logicAreaName")
                wave_name = record.get("fulfillmentWaveName")
                wave_id = map_wave_name_to_id(wave_name)
                if not warehouse or area not in ["调料", "总拣上料逻辑区"] or not wave_id:
                    continue
                key = (warehouse, wave_id)
                if key not in warehouse_bill_wave_counts:
                    warehouse_bill_wave_counts[key] = {"seasoning": set(), "feeding": set()}
                bill_no = record.get("zonePickBillNo")
                if area == "调料":
                    warehouse_bill_wave_counts[key]["seasoning"].add(bill_no)
                elif area == "总拣上料逻辑区":
                    warehouse_bill_wave_counts[key]["feeding"].add(bill_no)


            # ===================== 步骤2：统计各仓已拣数量（按履约产品） =====================
            warehouse_picked_wave = {}  # {(warehouse, wave_id): picked_volume}
            for record in picking_records:
                biz_bill_name = record.get("bizBillName")
                actual_qty = record.get("actualUnitTotalQty", 0)
                wave_name = record.get("fulfillmentWaveName")
                wave_id = map_wave_name_to_id(wave_name)
                if biz_bill_name and actual_qty > 0 and wave_id:
                    key = (biz_bill_name, wave_id)
                    warehouse_picked_wave[key] = warehouse_picked_wave.get(key, 0) + actual_qty

            # ===================== 步骤3：按人员汇总拣货数据 =====================
            picking_summary = {}
            target_areas = ["调料", "总拣上料逻辑区"]

            for record in picking_records:
                name = record.get("handlerName")
                area_name = record.get("logicAreaName", "")

                if not name or area_name not in target_areas:
                    continue

                qty = record.get("actualUnitTotalQty", 0)
                modify_time = record.get("lastModifyTime", 0)
                work_zone = area_name

                if name not in picking_summary:
                    picking_summary[name] = {
                        "handlerName": name,
                        "total_count": qty,
                        "feeding_area_count": qty if "总拣上料逻辑区" in work_zone else 0,
                        "seasoning_area_count": qty if "调料" in work_zone else 0,
                        "lastModifyTime": modify_time
                    }
                else:
                    picking_summary[name]["total_count"] += qty
                    if "总拣上料逻辑区" in work_zone:
                        picking_summary[name]["feeding_area_count"] += qty
                    elif "调料" in work_zone:
                        picking_summary[name]["seasoning_area_count"] += qty
                    if modify_time > picking_summary[name]["lastModifyTime"]:
                        picking_summary[name]["lastModifyTime"] = modify_time

            active_workers = list(picking_summary.values())
            print(f"[INFO] 筛选出 {len(active_workers)} 名活跃拣货人员")

            # ===================== 步骤3.5：统计每名员工在各仓库的拣货单数（按履约产品） =====================
            worker_wave_warehouse_bills = {}  # {name: {wave_id: {warehouse: set(bill_no)}}}
            for record in picking_records:
                name = record.get("handlerName")
                if not name:
                    continue
                warehouse = record.get("bizBillName")
                bill_no = record.get("zonePickBillNo")
                area = record.get("logicAreaName")
                wave_name = record.get("fulfillmentWaveName")
                wave_id = map_wave_name_to_id(wave_name)
                if area not in ["调料", "总拣上料逻辑区"] or not wave_id:
                    continue

                if name not in worker_wave_warehouse_bills:
                    worker_wave_warehouse_bills[name] = {}
                if wave_id not in worker_wave_warehouse_bills[name]:
                    worker_wave_warehouse_bills[name][wave_id] = {}
                if warehouse not in worker_wave_warehouse_bills[name][wave_id]:
                    worker_wave_warehouse_bills[name][wave_id][warehouse] = set()
                worker_wave_warehouse_bills[name][wave_id][warehouse].add(bill_no)

            # 写入员工-仓库-履约产品单量统计表
            c.execute("DELETE FROM worker_warehouse_wave_bill_count WHERE work_date = ?", (today_date,))
            for name, waves in worker_wave_warehouse_bills.items():
                for wave_id, warehouses in waves.items():
                    for warehouse, bills in warehouses.items():
                        c.execute('''
                            INSERT INTO worker_warehouse_wave_bill_count 
                            (name, work_date, warehouse_name, fulfillment_wave, bill_count)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (name, today_date, warehouse, wave_id, len(bills)))
            print(f"[INFO] 已更新 {len(worker_wave_warehouse_bills)} 名员工的仓库单量统计")

            # ===================== 步骤4：处理人员缓存和考勤 =====================
            for picking_data in active_workers:
                name = picking_data.get("handlerName")
                if not name:
                    continue

                # --- 4.1 检查人员档案缓存 ---
                c.execute("SELECT * FROM worker_info_cache WHERE name = ?", (name,))
                profile = c.fetchone()

                # 动态计算新员工标志
                if profile and profile['entry_time']:
                    entry_dt = datetime.strptime(profile['entry_time'], "%Y-%m-%d %H:%M:%S")
                    is_new_staff = (actual_now - entry_dt).days <= self.new_staff_days
                    if is_new_staff != profile['is_new_staff']:
                        c.execute("UPDATE worker_info_cache SET is_new_staff = ? WHERE name = ?", (is_new_staff, name))
                else:
                    is_new_staff = False

                if not profile:
                    print(f"[INFO] 发现未缓存档案：{name}，正在请求人员信息...")
                    api_user = "/hrm/labour/inhouse/user/r/pageUserList"
                    params_4 = {
                        "name": name,
                        "warehouseValidity": "EFFECTIVE",
                        "warehouseIdList": WAREHOUSE_ID,
                        "jobStatus": "INCUMBENCY",
                        "pageNo": 1,
                        "pageSize": 20,
                    }

                    res4 = request_with_retry(self.session, api_user, params_4)
                    if res4:
                        page_content = res4.get("data", {}).get("pageContent", [])
                        user_data = None
                        for item in page_content:
                            if item.get("name") == name:
                                user_data = item
                                break

                        if user_data:
                            entry_dt = self.parse_ms_timestamp(user_data.get("entryTime"))
                            is_new = (actual_now - entry_dt).days <= self.new_staff_days if entry_dt else False

                            # 判断是否为临时工
                            service_desc = str(user_data.get("labourServiceTypeDesc", ""))
                            is_temp = "临时" in service_desc

                            c.execute('''
                                INSERT INTO worker_info_cache 
                                (name, team_name, job_type, entry_time, is_new_staff, is_temp_worker) 
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (
                                name,
                                user_data.get("teamOrgName", "未知"),
                                user_data.get("jobNameDesc", "未知"),
                                entry_dt.strftime("%Y-%m-%d %H:%M:%S") if entry_dt else None,
                                is_new,
                                is_temp
                            ))

                    c.execute("SELECT * FROM worker_info_cache WHERE name = ?", (name,))
                    profile = c.fetchone()
                    time.sleep(0.2)

                # --- 4.2 检查今日考勤缓存 ---
                is_excluded = safe_get_row_value(profile, "is_excluded", 0)
                # 先算出当前停滞时间，用于决定是否需要查下班打卡
                _lm_ts = picking_data.get("lastModifyTime")
                _lm_dt = self.parse_ms_timestamp(_lm_ts)
                _idle = (actual_now - _lm_dt).total_seconds() / 60 if _lm_dt else 0

                c.execute("""
                    SELECT * FROM daily_attendance_cache 
                    WHERE name = ? AND attence_date = ?
                """, (name, today_date))
                attendance = c.fetchone()

                need_query_attendance = False
                if is_excluded:
                    pass  # 核心拦截：如果是标记的非拣货人员，彻底跳过考勤抓取
                elif not attendance:
                    need_query_attendance = True
                elif not safe_get_row_value(attendance, "clockin_time"):
                    need_query_attendance = True
                elif not safe_get_row_value(attendance, "clockout_time"):
                    if _idle >= 15 or actual_now.hour >= 22 or (actual_now.hour == 21 and actual_now.minute >= 30) or actual_now.hour < 8:
                        need_query_attendance = True
                else:
                    _ct = safe_get_row_value(attendance, "clockout_time")
                    try:
                        _ct_h = int(_ct[11:13])
                        _ct_m = int(_ct[14:16])
                        _ct_valid = _ct_h >= 22 or (_ct_h == 21 and _ct_m >= 30) or _ct_h <= 1
                    except Exception:
                        _ct_valid = False
                    if not _ct_valid and (actual_now.hour >= 22 or (actual_now.hour == 21 and actual_now.minute >= 30) or actual_now.hour < 8):
                        need_query_attendance = True

                # 限流：同一人 10 分钟内不重复查询考勤接口
                if need_query_attendance:
                    _last_q = self._attendance_last_query.get(name, 0)
                    if (time.time() - _last_q) < self.ATTENDANCE_QUERY_INTERVAL:
                        need_query_attendance = False  # 冷却中，跳过本轮

                if need_query_attendance:
                    self._attendance_last_query[name] = time.time()
                    print(f"[ATT] 正在查询 {name} ({picking_data.get('total_count', 0)}) 今日考勤...")
                    api_att = "/hrm/attendance/r/query"
                    params_1 = {
                        "warehouseValidity": "EFFECTIVE",
                        "warehouseId": WAREHOUSE_ID,
                        "labourUserName": name,
                        "attenceDate": current_month,
                        "pageNo": 1,
                        "pageSize": 200,
                    }

                    res1 = request_with_retry(self.session, api_att, params_1)
                    clockin_dt_str = None
                    clockout_dt_str = None

                    if res1:
                        page_content = res1.get("data", {}).get("pageContent", [])
                        for person_record in page_content:
                            if person_record.get("labourUserName") == name:
                                for day_data in person_record.get("attenceDayVoList", []):
                                    if day_data.get("attenceDate") == today_date:
                                        vos = day_data.get("attenceInfoVoList", [])
                                        if vos:
                                            # 上班时间：取第一个班次的 firstClockinTime
                                            t_str = vos[0].get("firstClockinTime")
                                            if t_str:
                                                dt_obj = datetime.strptime(f"{today_date} {t_str}", "%Y-%m-%d %H:%M")
                                                if dt_obj > actual_now:
                                                    dt_obj -= timedelta(days=1)
                                                clockin_dt_str = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
                                            # 下班时间：取最后一个班次的 endClockinTime
                                            last_end = vos[-1].get("endClockinTime")
                                            first_in = vos[0].get("firstClockinTime")
                                            if last_end and last_end != first_in:
                                                dt_out = datetime.strptime(f"{today_date} {last_end}", "%Y-%m-%d %H:%M")
                                                if dt_out > actual_now:
                                                    dt_out -= timedelta(days=1)
                                                clockout_dt_str = dt_out.strftime("%Y-%m-%d %H:%M:%S")
                                        break
                                break

                    c.execute("""
                        INSERT OR REPLACE INTO daily_attendance_cache 
                        (name, attence_date, clockin_time, clockout_time) 
                        VALUES (?, ?, ?, ?)
                    """, (name, today_date, clockin_dt_str, clockout_dt_str))

                c.execute("""
                    SELECT * FROM daily_attendance_cache 
                    WHERE name = ? AND attence_date = ?
                """, (name, today_date))
                attendance = c.fetchone()
                time.sleep(0.1)

                # ===================== 步骤5：计算人效并写入实时表 =====================
                clockin_time_str = safe_get_row_value(attendance, "clockin_time")
                # 给非拣货人员赋予虚拟打卡时间，防止被后续逻辑丢弃
                if is_excluded and not clockin_time_str:
                    clockin_time_str = f"{today_date} 20:00:00"
                if not clockin_time_str:
                    print(f"[INFO] {name} 今日无打卡记录，跳过写入")
                    continue

                # 停滞判定
                last_modify_ts = picking_data.get("lastModifyTime")
                last_modify = self.parse_ms_timestamp(last_modify_ts)
                is_stagnant = False
                idle_mins = 0

                if last_modify:
                    idle_mins = (actual_now - last_modify).total_seconds() / 60
                    is_stagnant = idle_mins > STAGNANT_MINUTES

                # 处理历史停顿次数和最长停顿
                c.execute(
                    "SELECT clockin_time, max_idle_minutes, stagnant_10min_count, last_counted_idle_ts FROM worker_realtime WHERE name = ?",
                    (name,))
                old_realtime = c.fetchone()

                old_clockin = safe_get_row_value(old_realtime, "clockin_time")

                # 如果是新的班次（打卡时间变了），必须清空昨天的历史停滞数据
                if old_clockin != clockin_time_str:
                    old_max_idle = 0
                    old_count = 0
                    last_counted_ts = ""
                else:
                    old_max_idle = safe_get_row_value(old_realtime, "max_idle_minutes", 0) or 0
                    old_count = safe_get_row_value(old_realtime, "stagnant_10min_count", 0) or 0
                    last_counted_ts = safe_get_row_value(old_realtime, "last_counted_idle_ts")

                # 过滤下班后的无限停滞
                if idle_mins < 120:
                    current_max_idle = max(old_max_idle, round(idle_mins, 1))
                else:
                    current_max_idle = old_max_idle

                current_count = old_count
                current_last_counted = last_counted_ts
                last_mod_str = last_modify.strftime("%Y-%m-%d %H:%M:%S") if last_modify else ""

                # 只有在正常的 10~120 分钟内的停顿，才算作一次有效的"摸鱼/异常"
                if 10 <= idle_mins < 120 and last_mod_str != current_last_counted:
                    current_count += 1
                    current_last_counted = last_mod_str

                # 计算工作时长
                clockin_dt = datetime.strptime(clockin_time_str, "%Y-%m-%d %H:%M:%S")
                end_dt = last_modify if last_modify else actual_now
                worked_seconds = (end_dt - clockin_dt).total_seconds()
                worked_hours = worked_seconds / 3600 if worked_seconds > 0 else 0.1

                # 计算人效
                total_count = picking_data.get("total_count", 0)
                efficiency = round(total_count / worked_hours, 2)
                feeding_area_count = picking_data.get("feeding_area_count", 0)
                seasoning_area_count = picking_data.get("seasoning_area_count", 0)

                # 写入实时表
                c.execute('''
                    INSERT OR REPLACE INTO worker_realtime 
                    (name, team_name, job_type, is_new_staff, clockin_time, 
                     last_modify_time, total_count, feeding_area_count, seasoning_area_count, 
                     efficiency, idle_minutes, is_stagnant, update_time,
                     max_idle_minutes, stagnant_10min_count, last_counted_idle_ts, is_temp_worker)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    name,
                    safe_get_row_value(profile, "team_name", "未知"),
                    safe_get_row_value(profile, "job_type", "未知"),
                    safe_get_row_value(profile, "is_new_staff", False),
                    clockin_time_str,
                    last_modify.strftime("%Y-%m-%d %H:%M:%S") if last_modify else None,
                    total_count,
                    feeding_area_count,
                    seasoning_area_count,
                    efficiency,
                    round(idle_mins, 1),
                    is_stagnant,
                    actual_now.strftime("%Y-%m-%d %H:%M:%S"),
                    current_max_idle,
                    current_count,
                    current_last_counted,
                    safe_get_row_value(profile, "is_temp_worker", False)
                ))

            # 清理今日无打卡记录的人员（一次性执行）
            c.execute("""
                DELETE FROM worker_realtime 
                WHERE name NOT IN (
                    SELECT DISTINCT name 
                    FROM daily_attendance_cache 
                    WHERE attence_date = ? AND clockin_time IS NOT NULL
                ) AND name NOT IN (
                    SELECT name FROM worker_info_cache WHERE is_excluded = 1
                )
            """, (today_date,))

            # 清理非拣货人员前一天的残留数据（打卡时间不是今天的说明是旧数据）
            c.execute("""
                DELETE FROM worker_realtime
                WHERE name IN (
                    SELECT name FROM worker_info_cache WHERE is_excluded = 1
                ) AND clockin_time NOT LIKE ?
            """, (f"{today_date}%",))

            # ===================== 步骤6：并行抓取仓储宏观数据（按履约产品） =====================
            print("[INFO] 正在并行拉取各仓按履约产品的实时调拨件量...")
            c.execute('''
                UPDATE global_warehouse_realtime_wave 
                SET volume = 0, need_allot_total_count = 0, 
                    picked_volume = 0, seasoning_bill_count = 0, feeding_bill_count = 0
            ''')
            api_allot = "/haina/obs/preAllocation/r/pageList"
            wave_ids = [1, 2, 5]

            def _fetch_wave_data(wid):
                """并行抓取单个波次的仓储宏观数据"""
                params = {
                    "warehouseId": WAREHOUSE_ID,
                    "appointmentDate": tomorrow_date,
                    "fulfillmentWaveId": wid,
                    "pageNo": 1,
                    "pageSize": 20,
                }
                res = request_with_retry(self.session, api_allot, params)
                if not res:
                    print(f"[WARN] 履约产品 {wid} 数据抓取失败，跳过")
                    return wid, []
                return wid, res.get("data", {}).get("totalStatistic", [])

            with ThreadPoolExecutor(max_workers=3) as executor:
                wave_futures = {executor.submit(_fetch_wave_data, wid): wid for wid in wave_ids}
                for fut in as_completed(wave_futures):
                    wave_id, total_statistics = fut.result()
                    for w_stat in total_statistics:
                        w_name = w_stat.get("toWarehouseName")
                        if not w_name:
                            continue
                        vol = w_stat.get("shouldAllotTotalCount", 0)
                        need_allot_count = w_stat.get("needAllotSkuTotalCount", 0)

                        c.execute('''
                            INSERT OR REPLACE INTO global_warehouse_realtime_wave 
                            (warehouse_name, fulfillment_wave, volume, need_allot_total_count,
                             picked_volume, seasoning_bill_count, feeding_bill_count)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (w_name, wave_id, vol, need_allot_count, 0, 0, 0))

            # 更新已拣数量和单量
            for (warehouse, wave_id), picked_vol in warehouse_picked_wave.items():
                c.execute('''
                    UPDATE global_warehouse_realtime_wave 
                    SET picked_volume = ?
                    WHERE warehouse_name = ? AND fulfillment_wave = ?
                ''', (picked_vol, warehouse, wave_id))

            for (warehouse, wave_id), counts in warehouse_bill_wave_counts.items():
                seasoning_bill_count = len(counts["seasoning"])
                feeding_bill_count = len(counts["feeding"])
                c.execute('''
                    UPDATE global_warehouse_realtime_wave 
                    SET seasoning_bill_count = ?, feeding_bill_count = ?
                    WHERE warehouse_name = ? AND fulfillment_wave = ?
                ''', (seasoning_bill_count, feeding_bill_count, warehouse, wave_id))

            # ===================== 步骤7：补查不在活跃列表中的停滞人员下班卡 =====================
            active_names = {pd.get("handlerName") for pd in active_workers if pd.get("handlerName")}
            c.execute("""
                SELECT wr.name, dac.clockout_time FROM worker_realtime wr
                LEFT JOIN daily_attendance_cache dac 
                    ON wr.name = dac.name AND dac.attence_date = ?
                LEFT JOIN worker_info_cache ic 
                    ON wr.name = ic.name
                WHERE wr.idle_minutes >= 15
                    AND COALESCE(ic.is_excluded, 0) = 0
                    AND (
                        dac.clockout_time IS NULL 
                        OR dac.clockout_time = ''
                        OR (
                            CAST(SUBSTR(dac.clockout_time, 12, 2) AS INTEGER) BETWEEN 2 AND 21
                            AND NOT (CAST(SUBSTR(dac.clockout_time, 12, 2) AS INTEGER) = 21 
                                     AND CAST(SUBSTR(dac.clockout_time, 15, 2) AS INTEGER) >= 30)
                        )
                    )
            """, (today_date,))
            stagnant_no_clockout = [row['name'] for row in c.fetchall() if row['name'] not in active_names]

            if stagnant_no_clockout:
                print(f"[INFO] 补查 {len(stagnant_no_clockout)} 名非活跃停滞人员的下班打卡: {stagnant_no_clockout}")
                for sname in stagnant_no_clockout:
                    # 限流：同一人 10 分钟内不重复查询考勤接口
                    _last_q = self._attendance_last_query.get(sname, 0)
                    if (time.time() - _last_q) < self.ATTENDANCE_QUERY_INTERVAL:
                        continue  # 冷却中，跳过
                    self._attendance_last_query[sname] = time.time()
                    try:
                        api_att = "/hrm/attendance/r/query"
                        params_att = {
                            "warehouseValidity": "EFFECTIVE",
                            "warehouseId": WAREHOUSE_ID,
                            "labourUserName": sname,
                            "attenceDate": current_month,
                            "pageNo": 1,
                            "pageSize": 200,
                        }
                        res_att = request_with_retry(self.session, api_att, params_att)
                        clockout_dt_str = None

                        if res_att:
                            page_content = res_att.get("data", {}).get("pageContent", [])
                            for person_record in page_content:
                                if person_record.get("labourUserName") == sname:
                                    for day_data in person_record.get("attenceDayVoList", []):
                                        if day_data.get("attenceDate") == today_date:
                                            vos = day_data.get("attenceInfoVoList", [])
                                            if vos:
                                                last_end = vos[-1].get("endClockinTime")
                                                first_in = vos[0].get("firstClockinTime")
                                                if last_end and last_end != first_in:
                                                    dt_out = datetime.strptime(f"{today_date} {last_end}", "%Y-%m-%d %H:%M")
                                                    if dt_out > actual_now:
                                                        dt_out -= timedelta(days=1)
                                                    clockout_dt_str = dt_out.strftime("%Y-%m-%d %H:%M:%S")
                                            break
                                    break

                        if clockout_dt_str:
                            c.execute("""
                                UPDATE daily_attendance_cache 
                                SET clockout_time = ?
                                WHERE name = ? AND attence_date = ?
                            """, (clockout_dt_str, sname, today_date))
                            print(f"  [OK] {sname} 补录下班打卡: {clockout_dt_str}")
                        else:
                            print(f"  [INFO] {sname} 暂无下班打卡记录")
                        time.sleep(0.2)
                    except Exception as e:
                        print(f"  [WARN] 补查 {sname} 考勤异常: {e}")

            conn.commit()
            print(f"[OK] [{actual_now.strftime('%H:%M:%S')}] 数据聚合成功，活跃人员：{len(active_workers)}人")

        except Exception as e:
            print(f"[ERROR] 抓取/处理过程异常: {e}")
            import traceback
            traceback.print_exc()
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()

    def fetch_abnormal_parcels(self):
        """抓取并分析包裹数量异常明细 (优化版：本地缓存防重 + 逻辑熔断)"""
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] [INFO] 开始智能巡检异常包裹...")

        target_warehouses = [349, 479, 501, 557, 636, 100, 189, 546, 616]

        actual_now = datetime.now()
        logical_today = actual_now - timedelta(days=1) if actual_now.hour < 8 else actual_now
        logical_tomorrow = logical_today + timedelta(days=1)
        appointment_date = logical_tomorrow.strftime("%Y-%m-%d")

        # 宽泛时间窗口，用于拣货单查询
        start_time = f"{logical_today.strftime('%Y-%m-%d')} 06:00:00"
        end_time = f"{logical_tomorrow.strftime('%Y-%m-%d')} 12:00:00"

        conn = get_db()
        c = conn.cursor()

        try:
            # 建立本地防重缓存池
            c.execute('''
                SELECT DISTINCT container_task_no 
                FROM abnormal_parcels 
                WHERE record_date = ? OR create_time >= ?
            ''', (appointment_date, logical_today.strftime('%Y-%m-%d 00:00:00')))

            existing_tasks = {row['container_task_no'] for row in c.fetchall()}
            if existing_tasks:
                print(f"   [INFO] 本地已缓存 {len(existing_tasks)} 个异常单，将自动跳过")

            for wh_id in target_warehouses:
                # 步骤 1: 抓取所有仓的包裹列表
                api_parcel_list = "/haina/ojs/rdc/r/containerParcelPrintList"
                params1 = {
                    "warehouseId": WAREHOUSE_ID,
                    "appointmentTime": appointment_date,
                    "allotInWarehouseId": wh_id,
                    "containerPrintTaskStatus": "",
                    "fulfillmentWaveId": "",
                    "isFulfillmentWaveGray": "true",
                    "zonePickBillNo": "",
                    "orderBy": "",
                    "asc": "true",
                    "pageNo": 1,
                    "pageSize": 200,
                    "wareHouseId": WAREHOUSE_ID,
                }

                res1 = request_with_retry(self.session, api_parcel_list, params1)
                if not res1 or not res1.get("data"):
                    continue

                page_content1 = res1["data"].get("pageContent", [])

                for task in page_content1:
                    parcel_qty = task.get("parcelQty")
                    sku_qty = task.get("skuQty")

                    # 基础过滤：没包裹 或 数量一致，跳过
                    if not parcel_qty or sku_qty == parcel_qty:
                        continue

                    task_no = task.get("containerPrintTaskNo")
                    zone_pick_bill_no = task.get("zonePickBillNo")

                    # 防重熔断
                    if task_no in existing_tasks:
                        continue

                    # 先查拣货单（宏观），确认是否已拣完
                    api_pick = "/haina/outbound/zonepick/r/pageList"
                    params3 = {
                        "createTimeStart": start_time,
                        "createTimeEnd": end_time,
                        "zonePickBillNo": zone_pick_bill_no,
                        "billType": "DB",
                        "deliveryRegionIds": "",
                        "pageNo": 1,
                        "pageSize": 20,
                        "wareHouseId": WAREHOUSE_ID,
                        "warehouseId": WAREHOUSE_ID,
                    }

                    res3 = request_with_retry(self.session, api_pick, params3)
                    if not res3 or not res3.get("data"):
                        continue

                    records3 = res3["data"].get("pageContent", [])
                    if not records3:
                        continue

                    rec = records3[0]

                    # 逻辑熔断：如果状态不是 "picked"，跳过
                    if rec.get("pickStatus") != "picked":
                        continue

                    # 只有确认为【已拣完】且有差异，才去抓取明细
                    api_detail = "/haina/ojs/rdc/r/containerParcelPrintDetail"
                    params2 = {
                        "allotInWarehouseId": wh_id,
                        "containerPrintTaskNo": task_no,
                        "warehouseId": WAREHOUSE_ID,
                        "wareHouseId": WAREHOUSE_ID,
                    }

                    res2 = request_with_retry(self.session, api_detail, params2)
                    if not res2 or not res2.get("data"):
                        continue

                    locations = res2["data"].get("locationDetails", [])
                    for loc in locations:
                        parcel_qty_raw = loc.get("parcelQty")
                        pick_qty_raw = loc.get("pickQty")

                        p_qty = int(parcel_qty_raw) if parcel_qty_raw else 0
                        pk_qty = int(pick_qty_raw) if pick_qty_raw else 0

                        if p_qty == 0 or pk_qty <= p_qty:
                            continue

                        sku_name = loc.get("skuName")
                        sku_code = loc.get("skuCode")
                        sku_brand = loc.get("skuBrand")
                        qty_diff = pk_qty - p_qty

                        c.execute('''
                            INSERT OR REPLACE INTO abnormal_parcels 
                            (record_date, container_task_no, zone_pick_bill_no, sku_name, qty_diff,
                             biz_bill_name, handler_name, fulfillment_wave_name, actual_sku_total_qty,
                             actual_unit_total_qty, allot_production_mode, receptacle_code, create_time,
                             sku_pick_qty, sku_parcel_qty, sku_code, sku_brand, allot_in_warehouse_id)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            appointment_date, task_no, zone_pick_bill_no, sku_name, qty_diff,
                            rec.get("bizBillName"), rec.get("handlerName"), rec.get("fulfillmentWaveName"),
                            rec.get("actualSkuTotalQty"), rec.get("actualUnitTotalQty"),
                            rec.get("allotProductionMode"), rec.get("receptacleCode"),
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            pk_qty, p_qty, str(sku_code) if sku_code else "", sku_brand, wh_id
                        ))

            conn.commit()
            print(f"[OK] 异常包裹巡检完成")
        except Exception as e:
            print(f"[ERROR] 异常包裹抓取失败: {e}")
            if conn: conn.rollback()
        finally:
            if conn: conn.close()

    def check_picking_stagnant(self):
        """
        拣货卡单监控，检测两种异常：
        1. 「拣完未提交」：实拣 >= 应拣，但 pickStatus 超过 3 分钟仍未变为 picked
        2. 「未完成停滞」：实拣 < 应拣，但 lastModifyTime 超过 3 分钟未更新
        """
        STAGNANT_MINUTES = 3

        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 检测拣货卡单...")

        actual_now = datetime.now()
        logical_today = actual_now - timedelta(days=1) if actual_now.hour < 8 else actual_now
        logical_tomorrow = logical_today + timedelta(days=1)
        today_date = logical_today.strftime("%Y-%m-%d")
        tomorrow_date = logical_tomorrow.strftime("%Y-%m-%d")

        api_pick = "/haina/outbound/zonepick/r/pageList"
        params = {
            "createTimeStart": f"{today_date} 06:00:00",
            "createTimeEnd": f"{tomorrow_date} 12:00:00",
            "pickStatus": "picking",
            "billType": "DB",
            "deliveryRegionIds": "",
            "pageNo": 1,
            "pageSize": 1000,
            "wareHouseId": WAREHOUSE_ID,
            "warehouseId": WAREHOUSE_ID,
        }

        res = request_with_retry(self.session, api_pick, params)
        if not res:
            print("拣货卡单检测：接口请求失败，本次跳过")
            return

        records = res.get("data", {}).get("pageContent", [])
        if not records:
            print("当前无拣货中的单据")
            records = []

        stagnant_list = []

        for rec in records:
            bill_no = rec.get("zonePickBillNo")
            if not bill_no:
                continue

            order_unit_qty = rec.get("orderUnitTotalQty") or 0
            actual_unit_qty = rec.get("actualUnitTotalQty") or 0

            last_modify_ts = rec.get("lastModifyTime")
            last_modify = self.parse_ms_timestamp(last_modify_ts)
            if not last_modify:
                continue

            # 过滤：实拣=0 且 领取时间距今不超过5分钟
            accept_ts = rec.get("acceptTime")
            accept_time = self.parse_ms_timestamp(accept_ts)
            if actual_unit_qty == 0 and accept_time:
                accept_age_mins = (actual_now - accept_time).total_seconds() / 60
                if accept_age_mins < 5:
                    continue

            stagnant_mins = (actual_now - last_modify).total_seconds() / 60
            if stagnant_mins < STAGNANT_MINUTES:
                continue

            if actual_unit_qty >= order_unit_qty:
                alert_reason = "拣完未提交"
            else:
                alert_reason = "未完成停滞"

            stagnant_list.append({
                "handler_name": rec.get("handlerName", "未知"),
                "zone_pick_bill_no": bill_no,
                "logic_area_name": rec.get("logicAreaName", ""),
                "biz_bill_name": rec.get("bizBillName", ""),
                "order_unit_qty": order_unit_qty,
                "actual_unit_qty": actual_unit_qty,
                "pick_status": rec.get("pickStatus", ""),
                "last_modify_time": last_modify.strftime("%Y-%m-%d %H:%M:%S"),
                "stagnant_minutes": round(stagnant_mins, 1),
                "alert_reason": alert_reason,
            })

        if not stagnant_list:
            print("无拣货卡单告警")
        else:
            cnt1 = sum(1 for x in stagnant_list if x["alert_reason"] == "拣完未提交")
            cnt2 = sum(1 for x in stagnant_list if x["alert_reason"] == "未完成停滞")
            print(f"发现告警 {len(stagnant_list)} 条（拣完未提交:{cnt1} 未完成停滞:{cnt2}），正在写入数据库...")

        conn = get_db()
        c = conn.cursor()
        try:
            for item in stagnant_list:
                c.execute('''
                    SELECT id FROM picking_stagnant_log
                    WHERE zone_pick_bill_no = ? AND is_resolved = 0
                ''', (item["zone_pick_bill_no"],))
                existing = c.fetchone()
                if existing:
                    c.execute('''
                        UPDATE picking_stagnant_log
                        SET stagnant_minutes = ?, record_time = ?,
                            actual_unit_qty = ?, alert_reason = ?,
                            last_modify_time = ?
                        WHERE id = ?
                    ''', (item["stagnant_minutes"], actual_now.strftime("%Y-%m-%d %H:%M:%S"),
                          item["actual_unit_qty"], item["alert_reason"],
                          item["last_modify_time"], existing["id"]))
                else:
                    c.execute('''
                        INSERT INTO picking_stagnant_log
                        (record_time, handler_name, zone_pick_bill_no, logic_area_name,
                         biz_bill_name, order_unit_qty, actual_unit_qty, pick_status,
                         last_modify_time, stagnant_minutes, alert_reason, is_resolved)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    ''', (
                        actual_now.strftime("%Y-%m-%d %H:%M:%S"),
                        item["handler_name"], item["zone_pick_bill_no"],
                        item["logic_area_name"], item["biz_bill_name"],
                        item["order_unit_qty"], item["actual_unit_qty"],
                        item["pick_status"], item["last_modify_time"],
                        item["stagnant_minutes"], item["alert_reason"]
                    ))

            # 自动解除逻辑
            still_stagnant_bills = {item["zone_pick_bill_no"] for item in stagnant_list}
            
            c.execute('SELECT id, zone_pick_bill_no FROM picking_stagnant_log WHERE is_resolved = 0')
            all_unresolved = c.fetchall()
            
            to_resolve_ids = []
            for row in all_unresolved:
                if row["zone_pick_bill_no"] not in still_stagnant_bills:
                    to_resolve_ids.append(row["id"])
            
            if to_resolve_ids:
                placeholders = ','.join(['?'] * len(to_resolve_ids))
                c.execute(f'''
                    UPDATE picking_stagnant_log
                    SET is_resolved = 1, resolve_time = ?
                    WHERE id IN ({placeholders})
                ''', [actual_now.strftime("%Y-%m-%d %H:%M:%S")] + to_resolve_ids)
                print(f"自动解除 {len(to_resolve_ids)} 条不再卡单的记录")

            conn.commit()
            print(f"拣货卡单写入完成，共 {len(stagnant_list)} 条")
        except Exception as e:
            print(f"拣货卡单写入失败: {e}")
            conn.rollback()
        finally:
            conn.close()


# ===================== 程序入口 =====================
if __name__ == "__main__":
    try:
        spider = WMSDataSpider()
        spider.process_and_save()
        spider.fetch_abnormal_parcels()
        spider.check_picking_stagnant()
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"\n程序启动失败: {e}")
        import traceback
        traceback.print_exc()
