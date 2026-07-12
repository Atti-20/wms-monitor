# -*- coding: utf-8 -*-
"""
拣货数据抓取与人效计算模块
对应原 WMSDataSpider.process_and_save()
"""
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from database import get_db
from spiders.base import (
    request_with_retry, safe_get_row_value, map_wave_name_to_id,
    parse_ms_timestamp, WAREHOUSE_ID, NEW_STAFF_DAYS, STAGNANT_MINUTES,
    get_shared_session
)

# 考勤查询限流：每人至少间隔 10 分钟才重新查询
ATTENDANCE_QUERY_INTERVAL = 600
_attendance_last_query = {}

# 每日人员标签刷新：记录最后一次刷新的逻辑日期，确保每天只刷新一次
_last_label_refresh_date = None

def _daily_refresh_worker_labels(session, actual_now, logical_date, active_names=None):
    """
    每天第一次运行时，批量刷新今日有拣货记录人员的标签。
    临时工/固定工身份(is_temp_worker)、新人标志(is_new_staff)、班组(team_name)
    都可能随时间变化，需要每天重新从 API 拉取。

    仅刷新今日实际有拣货记录的人员（active_names），而非 worker_info_cache 全量人员，
    避免每天对数百名无拣货记录的历史人员发起无意义的 API 调用。
    若 active_names 为空或未传入，则跳过刷新。

    为减少 database is locked 风险，采用"批量读 → 逐人API → 批量写"模式：
    1. 短暂开连接读取所有缓存人员名单
    2. 逐人调API（不持有连接）
    3. 短暂开连接批量写入更新
    """
    global _last_label_refresh_date
    if _last_label_refresh_date == logical_date:
        return  # 今天已经刷新过
    _last_label_refresh_date = logical_date

    if not active_names:
        print("[标签刷新] 今日无拣货记录人员，跳过")
        return

    # 步骤1：短暂开连接读取人员名单，仅保留今日有拣货记录的人员
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT name, entry_time FROM worker_info_cache")
    all_cached = [dict(row) for row in c.fetchall() if row["name"] in active_names]
    conn.close()

    if not all_cached:
        print("[标签刷新] 今日拣货人员均不在 worker_info_cache 中，跳过")
        return

    print(f"[标签刷新] 每日首次运行，开始刷新 {len(all_cached)} 名今日有拣货记录员工的身份标签...")
    api_user = "/hrm/labour/inhouse/user/r/pageUserList"

    # 步骤2：逐人调API，收集更新数据到内存
    updates = []
    for row in all_cached:
        name = row["name"]
        params = {
            "name": name,
            "warehouseValidity": "EFFECTIVE",
            "warehouseIdList": WAREHOUSE_ID,
            "jobStatus": "INCUMBENCY",
            "pageNo": 1,
            "pageSize": 20,
        }
        try:
            res = request_with_retry(session, api_user, params)
            if not res:
                continue
            page_content = res.get("data", {}).get("pageContent", [])
            user_data = None
            for item in page_content:
                if item.get("name") == name:
                    user_data = item
                    break
            if not user_data:
                continue

            # 重新计算各标签
            entry_dt = parse_ms_timestamp(user_data.get("entryTime"))
            is_new = (actual_now - entry_dt).days <= NEW_STAFF_DAYS if entry_dt else False
            service_desc = str(user_data.get("labourServiceTypeDesc", ""))
            is_temp = "临时" in service_desc
            new_team = user_data.get("teamOrgName", "未知")
            new_job = user_data.get("jobNameDesc", "未知")
            new_entry = entry_dt.strftime("%Y-%m-%d %H:%M:%S") if entry_dt else None

            updates.append((is_new, is_temp, new_team, new_job, new_entry, name))
            time.sleep(0.15)  # 限流，避免 API 压力过大
        except Exception as e:
            print(f"[标签刷新] 刷新 {name} 失败: {e}")

    # 步骤3：短暂开连接批量写入
    if updates:
        conn = get_db()
        c = conn.cursor()
        for upd in updates:
            c.execute('''
                UPDATE worker_info_cache
                SET is_new_staff = ?, is_temp_worker = ?, team_name = ?, job_type = ?, entry_time = ?
                WHERE name = ?
            ''', upd)
        conn.commit()
        conn.close()

    print(f"[标签刷新] 完成，共更新 {len(updates)}/{len(all_cached)} 名员工标签")


# ===== 拣货逻辑区配置 =====
# 业务侧逻辑区(logicAreaName)曾改版重命名，此处集中维护，改名只需改这里。
# FEEDING_AREAS：上料类逻辑区（前端展示为"上料"，对应字段 feeding_area_count）
#   G1、G4 归为上料
# SEASONING_AREAS：米面类逻辑区（前端展示为"米面"，对应字段 seasoning_area_count）
#   G2、G3 归为米面
# 注意："鸡蛋拣货逻辑区"按业务要求不纳入人效统计，故不在以下任何集合中。
FEEDING_AREAS = {"上料小件G1拣货区", "爆品饮料G4拣货区"}
SEASONING_AREAS = {"酒饮米面G2拣货区", "爆品米面G3拣货区"}
# 参与人效统计的全部逻辑区
TARGET_AREAS = FEEDING_AREAS | SEASONING_AREAS


def process_and_save():
    """核心逻辑：抓取数据、处理缓存、计算人效、写入数据库
    
    架构说明：为避免 SQLite 'database is locked' 错误，本函数严格分为两个阶段：
    阶段一（纯网络IO）：通过 API 抓取所有原始数据，存入内存
    阶段二（纯数据库IO）：打开连接 → 快速写入 → 立即关闭
    这样数据库连接持有时间从数十秒缩短到数百毫秒。
    """
    session = get_shared_session()
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] [START] 触发智能抓取与缓存融合计算...")

    actual_now = datetime.now()
    logical_today = actual_now - timedelta(days=1) if actual_now.hour < 8 else actual_now
    logical_tomorrow = logical_today + timedelta(days=1)
    current_month = logical_today.strftime("%Y-%m")
    today_date = logical_today.strftime("%Y-%m-%d")
    tomorrow_date = logical_tomorrow.strftime("%Y-%m-%d")

    # ================================================================
    # 阶段一：纯网络IO —— 抓取所有API数据到内存，不持有数据库连接
    # ================================================================

    # ===== 步骤1 + 1.5 并行：拣货数据 & 已生成拣货单 =====
    api_pick = "/haina/outbound/zonepick/r/pageList"
    base_params = {
        "createTimeStart": f"{today_date} 06:00:00",
        "createTimeEnd": f"{tomorrow_date} 12:00:00",
        "billType": "DB",
        "deliveryRegionIds": "",
        "pageSize": 200,
        "wareHouseId": WAREHOUSE_ID,
        "warehouseId": WAREHOUSE_ID,
    }

    def _fetch_picking_records():
        all_records = []
        pg = 1
        while True:
            params = {**base_params, "pageNo": pg}
            res = request_with_retry(session, api_pick, params)
            if not res:
                if pg == 1:
                    return None
                break
            page_content = res.get("data", {}).get("pageContent", [])
            if not page_content:
                break
            all_records.extend(page_content)
            total_pages = res.get("data", {}).get("page", {}).get("totalPageCount", 1)
            if pg >= total_pages:
                break
            pg += 1
        print(f"[INFO] 抓取到 {len(all_records)} 条拣货明细记录(共{pg}页)")
        return all_records

    def _fetch_created_records():
        params = {**base_params, "pickStatus": "created", "pageNo": 1}
        all_records = []
        pg = 1
        while True:
            params["pageNo"] = pg
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
        print(f"[INFO] 获取到 {len(all_records)} 条已生成的拣货明细记录")
        return all_records

    def _fetch_picking_status_count():
        """查询 pickStatus=picking 的拣货中任务数量（只取第一页拿 totalCount）"""
        params = {**base_params, "pickStatus": "picking", "pageNo": 1, "pageSize": 1}
        res = request_with_retry(session, api_pick, params)
        if not res:
            return 0
        total = res.get("data", {}).get("page", {}).get("totalCount", 0)
        print(f"[INFO] pickStatus=picking 任务数: {total}")
        return total

    def _fetch_allocation_created_count():
        """查询调拨单 outboundBillStatus=CREATED 的数量"""
        api_alloc = "/haina/outbound/allocation/r/pageList"
        params = {
            "createTimeStart": f"{today_date} 00:00:00",
            "createTimeEnd": f"{tomorrow_date} 23:59:59",
            "outboundBillStatus": "CREATED",
            "allotProductionMode": "BALANCE_PRODUCTION",
            "pageNo": 1,
            "pageSize": 1,
            "wareHouseId": WAREHOUSE_ID,
            "warehouseId": WAREHOUSE_ID,
        }
        res = request_with_retry(session, api_alloc, params)
        if not res:
            return 0
        total = res.get("data", {}).get("page", {}).get("totalCount", 0)
        print(f"[INFO] 调拨单 CREATED 数量: {total}")
        return total

    with ThreadPoolExecutor(max_workers=4) as executor:
        fut_pick = executor.submit(_fetch_picking_records)
        fut_created = executor.submit(_fetch_created_records)
        fut_picking_count = executor.submit(_fetch_picking_status_count)
        fut_alloc_created = executor.submit(_fetch_allocation_created_count)
        picking_records = fut_pick.result()
        all_created_records = fut_created.result()
        picking_status_count = fut_picking_count.result()
        allocation_created_count = fut_alloc_created.result()

    if picking_records is None:
        print("[ERROR] 拣货数据抓取失败，本次轮询跳过")
        return
    if all_created_records is None:
        all_created_records = []

    # 统计各仓单量
    warehouse_bill_wave_counts = {}
    for record in all_created_records:
        warehouse = record.get("bizBillName")
        area = record.get("logicAreaName")
        wave_name = record.get("fulfillmentWaveName")
        wave_id = map_wave_name_to_id(wave_name)
        if not warehouse or area not in TARGET_AREAS or not wave_id:
            continue
        key = (warehouse, wave_id)
        if key not in warehouse_bill_wave_counts:
            warehouse_bill_wave_counts[key] = {"seasoning": set(), "feeding": set()}
        bill_no = record.get("zonePickBillNo")
        if area in SEASONING_AREAS:
            warehouse_bill_wave_counts[key]["seasoning"].add(bill_no)
        elif area in FEEDING_AREAS:
            warehouse_bill_wave_counts[key]["feeding"].add(bill_no)

    # ===== 步骤2：统计各仓已拣数量 & 分区件量 =====
    warehouse_picked_wave = {}
    # G1-G4的未拣量（应拣-已拣）和已拣量
    target_area_unpicked = 0
    target_area_picked = 0  # G1-G4已拣总量
    egg_actual_total = 0  # 鸡蛋区已拣总量
    wave_total_picked = {}  # {wave_id: 总已拣量}（G1-G4 + 鸡蛋，用于按波次快照）
    for record in picking_records:
        biz_bill_name = record.get("bizBillName")
        actual_qty = record.get("actualUnitTotalQty", 0)
        order_qty = record.get("orderUnitTotalQty", 0)
        wave_name = record.get("fulfillmentWaveName")
        wave_id = map_wave_name_to_id(wave_name)
        area = record.get("logicAreaName", "")
        # 统计G1-G4未拣量和已拣量
        if area in TARGET_AREAS:
            target_area_unpicked += max(0, order_qty - actual_qty)
            target_area_picked += actual_qty
        else:
            # 鸡蛋等非目标区的已拣
            egg_actual_total += actual_qty
        if biz_bill_name and actual_qty > 0 and wave_id:
            key = (biz_bill_name, wave_id)
            warehouse_picked_wave[key] = warehouse_picked_wave.get(key, 0) + actual_qty
        if wave_id and actual_qty > 0:
            wave_total_picked[wave_id] = wave_total_picked.get(wave_id, 0) + actual_qty

    # 总已拣 = G1-G4已拣 + 鸡蛋已拣
    total_all_picked = target_area_picked + egg_actual_total
    print(f"[INFO] G1-G4已拣: {target_area_picked}, G1-G4未拣: {target_area_unpicked}, 鸡蛋已拣: {egg_actual_total}, 总已拣: {total_all_picked}")
    print(f"[INFO] 拣货中任务: {picking_status_count}, 已生成任务: {len(all_created_records)}, 待执行调拨单: {allocation_created_count}")

    # ===== 拣货进度快照（10分钟粒度）=====
    now_ts = datetime.now()
    slot_minute = (now_ts.minute // 10) * 10
    time_slot = f"{now_ts.hour:02d}:{slot_minute:02d}"

    # ===== 步骤3：按人员汇总拣货数据 =====
    picking_summary = {}
    target_areas = TARGET_AREAS

    for record in picking_records:
        name = record.get("handlerName")
        area_name = record.get("logicAreaName", "")
        if not name or area_name not in target_areas:
            continue
        qty = record.get("actualUnitTotalQty", 0)
        modify_time = record.get("lastModifyTime", 0)

        # 计算单任务拣货时长（completeTime - acceptTime）
        accept_ts = record.get("acceptTime", 0)
        complete_ts = record.get("completeTime", 0)
        task_duration_sec = 0
        if accept_ts and complete_ts and complete_ts > accept_ts:
            task_duration_sec = (complete_ts - accept_ts) / 1000  # 毫秒转秒

        if name not in picking_summary:
            picking_summary[name] = {
                "handlerName": name,
                "total_count": qty,
                "feeding_area_count": qty if area_name in FEEDING_AREAS else 0,
                "seasoning_area_count": qty if area_name in SEASONING_AREAS else 0,
                "lastModifyTime": modify_time,
                "total_task_duration_sec": task_duration_sec,
            }
        else:
            picking_summary[name]["total_count"] += qty
            if area_name in FEEDING_AREAS:
                picking_summary[name]["feeding_area_count"] += qty
            elif area_name in SEASONING_AREAS:
                picking_summary[name]["seasoning_area_count"] += qty
            if modify_time > picking_summary[name]["lastModifyTime"]:
                picking_summary[name]["lastModifyTime"] = modify_time
            picking_summary[name]["total_task_duration_sec"] += task_duration_sec

    active_workers = list(picking_summary.values())
    print(f"[INFO] 筛选出 {len(active_workers)} 名活跃拣货人员")

    # ===== 步骤3.5：统计每名员工在各仓库的拣货单数 =====
    worker_wave_warehouse_bills = {}
    # 按逻辑区统计每名员工的拣货单数（去重 billNo）
    worker_area_bills = {}  # {name: {'seasoning': set(), 'feeding': set()}}
    for record in picking_records:
        name = record.get("handlerName")
        if not name:
            continue
        warehouse = record.get("bizBillName")
        bill_no = record.get("zonePickBillNo")
        area = record.get("logicAreaName")
        wave_name = record.get("fulfillmentWaveName")
        wave_id = map_wave_name_to_id(wave_name)
        if area not in TARGET_AREAS or not wave_id:
            continue
        if name not in worker_wave_warehouse_bills:
            worker_wave_warehouse_bills[name] = {}
        if wave_id not in worker_wave_warehouse_bills[name]:
            worker_wave_warehouse_bills[name][wave_id] = {}
        if warehouse not in worker_wave_warehouse_bills[name][wave_id]:
            worker_wave_warehouse_bills[name][wave_id][warehouse] = set()
        worker_wave_warehouse_bills[name][wave_id][warehouse].add(bill_no)
        # 按逻辑区统计单数
        if name not in worker_area_bills:
            worker_area_bills[name] = {'seasoning': set(), 'feeding': set()}
        if area in SEASONING_AREAS:
            worker_area_bills[name]['seasoning'].add(bill_no)
        elif area in FEEDING_AREAS:
            worker_area_bills[name]['feeding'].add(bill_no)

    # ===== 步骤6：并行抓取仓储宏观数据（纯网络IO，不需要数据库）=====
    print("[INFO] 正在并行拉取各仓按履约产品的实时调拨件量...")
    api_allot = "/haina/obs/preAllocation/r/pageList"
    wave_ids = [1, 2, 5]

    def _fetch_wave_data(wid):
        params = {
            "warehouseId": WAREHOUSE_ID,
            "appointmentDate": tomorrow_date,
            "fulfillmentWaveId": wid,
            "pageNo": 1,
            "pageSize": 20,
        }
        res = request_with_retry(session, api_allot, params)
        if not res:
            print(f"[WARN] 履约产品 {wid} 数据抓取失败，跳过")
            return wid, []
        return wid, res.get("data", {}).get("totalStatistic", [])

    with ThreadPoolExecutor(max_workers=3) as executor:
        wave_futures = {executor.submit(_fetch_wave_data, wid): wid for wid in wave_ids}
        wave_results = {}
        for fut in as_completed(wave_futures):
            wave_id_result, total_statistics = fut.result()
            wave_results[wave_id_result] = total_statistics

    # ================================================================
    # 阶段二：纯数据库IO —— 打开连接、快速写入、立即关闭
    # 所有网络请求已经完成，下面只做内存数据 → 数据库的写入
    # ================================================================
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        # -- 写入 kv_store --
        c.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                  ("target_area_unpicked", str(target_area_unpicked)))
        c.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                  ("target_area_picked", str(target_area_picked)))
        c.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                  ("egg_actual_total", str(egg_actual_total)))
        c.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                  ("total_all_picked", str(total_all_picked)))
        c.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                  ("pick_status_picking_count", str(picking_status_count)))
        c.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                  ("pick_status_created_count", str(len(all_created_records))))
        c.execute("INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
                  ("allocation_created_count", str(allocation_created_count)))

        # -- 写入拣货进度快照 --
        c.execute('''INSERT OR REPLACE INTO picking_progress_snapshot
                     (logical_date, time_slot, total_picked, total_unpicked, snapshot_time)
                     VALUES (?, ?, ?, ?, ?)''',
                  (today_date, time_slot, total_all_picked, target_area_unpicked,
                   now_ts.strftime("%Y-%m-%d %H:%M:%S")))

        # -- 写入按波次拆分的拣货进度快照（凌晨达/上午达/下午达）--
        WAVE_ID_TO_NAME = {1: "上午达", 2: "下午达", 5: "凌晨达"}
        for w_id, w_name in WAVE_ID_TO_NAME.items():
            w_picked = wave_total_picked.get(w_id, 0)
            c.execute('''INSERT OR REPLACE INTO picking_progress_snapshot_wave
                         (logical_date, time_slot, wave_name, total_picked, snapshot_time)
                         VALUES (?, ?, ?, ?, ?)''',
                      (today_date, time_slot, w_name, w_picked,
                       now_ts.strftime("%Y-%m-%d %H:%M:%S")))

        # -- 写入员工仓库单量统计 --
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

        # -- 写入仓储宏观数据 --
        c.execute('''UPDATE global_warehouse_realtime_wave 
                     SET volume = 0, need_allot_total_count = 0, 
                         picked_volume = 0, seasoning_bill_count = 0, feeding_bill_count = 0''')
        for wave_id_r, total_statistics in wave_results.items():
            for w_stat in total_statistics:
                w_name = w_stat.get("toWarehouseName")
                if not w_name:
                    continue
                vol = w_stat.get("shouldAllotTotalCount", 0)
                need_allot_count = w_stat.get("needAllotSkuTotalCount", 0)
                c.execute('''INSERT OR REPLACE INTO global_warehouse_realtime_wave 
                             (warehouse_name, fulfillment_wave, volume, need_allot_total_count,
                              picked_volume, seasoning_bill_count, feeding_bill_count)
                             VALUES (?, ?, ?, ?, ?, ?, ?)''',
                          (w_name, wave_id_r, vol, need_allot_count, 0, 0, 0))

        for (warehouse, wave_id), picked_vol in warehouse_picked_wave.items():
            c.execute('''UPDATE global_warehouse_realtime_wave 
                         SET picked_volume = ?
                         WHERE warehouse_name = ? AND fulfillment_wave = ?''',
                      (picked_vol, warehouse, wave_id))

        for (warehouse, wave_id), counts in warehouse_bill_wave_counts.items():
            seasoning_bill_count = len(counts["seasoning"])
            feeding_bill_count = len(counts["feeding"])
            c.execute('''UPDATE global_warehouse_realtime_wave 
                         SET seasoning_bill_count = ?, feeding_bill_count = ?
                         WHERE warehouse_name = ? AND fulfillment_wave = ?''',
                      (seasoning_bill_count, feeding_bill_count, warehouse, wave_id))

        conn.commit()
        conn.close()
        conn = None
        print("[INFO] 阶段二-批次1完成：kv_store/快照/仓库统计/宏观数据已写入")

        # ===== 步骤3.5：每日首次运行刷新人员标签（需要API+DB交叉操作，单独开连接）=====
        # 只刷新今日实际有拣货记录的人员，而非全量 worker_info_cache（可能数百人），减少无意义的API调用
        active_names_for_label = {pd.get("handlerName") for pd in active_workers if pd.get("handlerName")}
        _daily_refresh_worker_labels(session, actual_now, logical_today.strftime("%Y-%m-%d"), active_names_for_label)

        # ===== 步骤4：处理人员缓存和考勤（需要逐人查缓存+可能调API，单独事务）=====
        conn = get_db()
        c = conn.cursor()

        for picking_data in active_workers:
            name = picking_data.get("handlerName")
            if not name:
                continue

            c.execute("SELECT * FROM worker_info_cache WHERE name = ?", (name,))
            profile = c.fetchone()

            if profile and profile['entry_time']:
                entry_dt = datetime.strptime(profile['entry_time'], "%Y-%m-%d %H:%M:%S")
                is_new_staff = (actual_now - entry_dt).days <= NEW_STAFF_DAYS
                if is_new_staff != profile['is_new_staff']:
                    c.execute("UPDATE worker_info_cache SET is_new_staff = ? WHERE name = ?", (is_new_staff, name))
            else:
                is_new_staff = False

            if not profile:
                print(f"[INFO] 发现未缓存档案：{name}，正在请求人员信息...")
                # 先提交当前事务、关闭连接，再做网络请求
                conn.commit()
                conn.close()
                conn = None

                api_user = "/hrm/labour/inhouse/user/r/pageUserList"
                params_4 = {
                    "name": name,
                    "warehouseValidity": "EFFECTIVE",
                    "warehouseIdList": WAREHOUSE_ID,
                    "jobStatus": "INCUMBENCY",
                    "pageNo": 1,
                    "pageSize": 20,
                }
                res4 = request_with_retry(session, api_user, params_4)
                time.sleep(0.2)

                # 网络请求完成后重新打开连接
                conn = get_db()
                c = conn.cursor()

                if res4:
                    page_content = res4.get("data", {}).get("pageContent", [])
                    user_data = None
                    for item in page_content:
                        if item.get("name") == name:
                            user_data = item
                            break
                    if user_data:
                        entry_dt = parse_ms_timestamp(user_data.get("entryTime"))
                        is_new = (actual_now - entry_dt).days <= NEW_STAFF_DAYS if entry_dt else False
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

            # --- 4.2 检查今日考勤缓存 ---
            is_excluded = safe_get_row_value(profile, "is_excluded", 0)
            _lm_ts = picking_data.get("lastModifyTime")
            _lm_dt = parse_ms_timestamp(_lm_ts)
            _idle = (actual_now - _lm_dt).total_seconds() / 60 if _lm_dt else 0

            c.execute("""
                SELECT * FROM daily_attendance_cache 
                WHERE name = ? AND attence_date = ?
            """, (name, today_date))
            attendance = c.fetchone()

            need_query_attendance = False
            if is_excluded:
                pass
            elif not attendance:
                need_query_attendance = True
            elif not safe_get_row_value(attendance, "clockin_time"):
                need_query_attendance = True
            elif not safe_get_row_value(attendance, "clockout_time"):
                if _idle >= 10 or actual_now.hour >= 22 or (actual_now.hour == 21 and actual_now.minute >= 30) or actual_now.hour < 8:
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

            if need_query_attendance:
                _last_q = _attendance_last_query.get(name, 0)
                if (time.time() - _last_q) < ATTENDANCE_QUERY_INTERVAL:
                    need_query_attendance = False

            if need_query_attendance:
                _attendance_last_query[name] = time.time()
                print(f"[ATT] 正在查询 {name} ({picking_data.get('total_count', 0)}) 今日考勤...")

                # 先提交并关闭连接，再做网络请求
                conn.commit()
                conn.close()
                conn = None

                api_att = "/hrm/attendance/r/query"
                params_1 = {
                    "warehouseValidity": "EFFECTIVE",
                    "warehouseId": WAREHOUSE_ID,
                    "labourUserName": name,
                    "attenceDate": current_month,
                    "pageNo": 1,
                    "pageSize": 200,
                }
                res1 = request_with_retry(session, api_att, params_1)
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
                                        t_str = vos[0].get("firstClockinTime")
                                        if t_str:
                                            dt_obj = datetime.strptime(f"{today_date} {t_str}", "%Y-%m-%d %H:%M")
                                            if dt_obj > actual_now:
                                                dt_obj -= timedelta(days=1)
                                            clockin_dt_str = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
                                        last_end = vos[-1].get("endClockinTime")
                                        first_in = vos[0].get("firstClockinTime")
                                        if last_end and last_end != first_in:
                                            dt_out = datetime.strptime(f"{today_date} {last_end}", "%Y-%m-%d %H:%M")
                                            if dt_out > actual_now:
                                                dt_out -= timedelta(days=1)
                                            clockout_dt_str = dt_out.strftime("%Y-%m-%d %H:%M:%S")
                                    break
                            break

                # 网络请求完成，重新打开连接写入考勤数据
                conn = get_db()
                c = conn.cursor()

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

            # ===== 步骤5：计算人效并写入实时表 =====
            # 只要该人员今日有拣货记录（在 active_workers 中）就统计，不再因缺少打卡记录而跳过。
            # clockin_time_str 可能为空（无打卡记录），后续综合效率计算会做兼容处理。
            clockin_time_str = safe_get_row_value(attendance, "clockin_time")
            if is_excluded and not clockin_time_str:
                clockin_time_str = f"{today_date} 20:00:00"
            if not clockin_time_str:
                print(f"[INFO] {name} 今日无打卡记录，但有拣货记录，仍纳入统计")

            last_modify_ts = picking_data.get("lastModifyTime")
            last_modify = parse_ms_timestamp(last_modify_ts)
            is_stagnant = False
            idle_mins = 0

            if last_modify:
                idle_mins = (actual_now - last_modify).total_seconds() / 60
                is_stagnant = idle_mins > STAGNANT_MINUTES

            c.execute(
                "SELECT clockin_time, max_idle_minutes, stagnant_10min_count, last_counted_idle_ts FROM worker_realtime WHERE name = ?",
                (name,))
            old_realtime = c.fetchone()
            old_clockin = safe_get_row_value(old_realtime, "clockin_time")

            if old_clockin != clockin_time_str:
                old_max_idle = 0
                old_count = 0
                last_counted_ts = ""
            else:
                old_max_idle = safe_get_row_value(old_realtime, "max_idle_minutes", 0) or 0
                old_count = safe_get_row_value(old_realtime, "stagnant_10min_count", 0) or 0
                last_counted_ts = safe_get_row_value(old_realtime, "last_counted_idle_ts")

            if idle_mins < 120:
                current_max_idle = max(old_max_idle, round(idle_mins, 1))
            else:
                current_max_idle = old_max_idle

            current_count = old_count
            current_last_counted = last_counted_ts
            last_mod_str = last_modify.strftime("%Y-%m-%d %H:%M:%S") if last_modify else ""

            if 10 <= idle_mins < 120 and last_mod_str != current_last_counted:
                current_count += 1
                current_last_counted = last_mod_str

            total_count = picking_data.get("total_count", 0)
            # 效率 = 总件数 / 拣货任务总时长（各任务 completeTime - acceptTime 之和）
            total_task_duration_sec = picking_data.get("total_task_duration_sec", 0)
            if total_task_duration_sec > 0:
                task_hours = total_task_duration_sec / 3600
                efficiency = round(total_count / task_hours, 2)
            elif clockin_time_str:
                # 无任务时长数据时（如全部拣货中），回退到打卡时长
                clockin_dt = datetime.strptime(clockin_time_str, "%Y-%m-%d %H:%M:%S")
                end_dt = last_modify if last_modify else actual_now
                worked_seconds = (end_dt - clockin_dt).total_seconds()
                worked_hours = worked_seconds / 3600 if worked_seconds > 0 else 0.1
                efficiency = round(total_count / worked_hours, 2)
            else:
                # 既无任务时长数据，又无打卡记录，无法计算工时，效率置0（不影响件量统计）
                efficiency = 0
            feeding_area_count = picking_data.get("feeding_area_count", 0)
            seasoning_area_count = picking_data.get("seasoning_area_count", 0)

            # 获取该员工的米面/上料单数
            area_bills = worker_area_bills.get(name, {'seasoning': set(), 'feeding': set()})
            seasoning_bill_count = len(area_bills['seasoning'])
            feeding_bill_count = len(area_bills['feeding'])

            c.execute('''
                INSERT OR REPLACE INTO worker_realtime 
                (name, team_name, job_type, is_new_staff, clockin_time, 
                 last_modify_time, total_count, feeding_area_count, seasoning_area_count, 
                 efficiency, idle_minutes, is_stagnant, update_time,
                 max_idle_minutes, stagnant_10min_count, last_counted_idle_ts, is_temp_worker,
                 seasoning_bill_count, feeding_bill_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                safe_get_row_value(profile, "is_temp_worker", False),
                seasoning_bill_count,
                feeding_bill_count
            ))

        # -- 提交 worker_realtime + daily_attendance_cache 批量更新 --
        conn.commit()

        # 清理今日既无拣货记录、又非排除人员的残留数据。
        # 注意：不再要求必须有打卡记录——只要今日有拣货记录（在 active_workers 中）就予以保留统计。
        active_names_list = sorted({pd.get("handlerName") for pd in active_workers if pd.get("handlerName")})
        if active_names_list:
            placeholders = ",".join("?" for _ in active_names_list)
            c.execute(f"""
                DELETE FROM worker_realtime 
                WHERE name NOT IN ({placeholders})
                AND name NOT IN (
                    SELECT name FROM worker_info_cache WHERE is_excluded = 1
                )
            """, active_names_list)
        else:
            # 本次没有任何活跃拣货人员时，清空所有非排除人员的残留数据
            c.execute("""
                DELETE FROM worker_realtime 
                WHERE name NOT IN (
                    SELECT name FROM worker_info_cache WHERE is_excluded = 1
                )
            """)

        # 清理非拣货人员（is_excluded=1）前一天的残留数据，此类人员仍以打卡时间为准
        c.execute("""
            DELETE FROM worker_realtime
            WHERE name IN (
                SELECT name FROM worker_info_cache WHERE is_excluded = 1
            ) AND (clockin_time IS NULL OR clockin_time NOT LIKE ?)
        """, (f"{today_date}%",))

        conn.commit()
        conn.close()
        conn = None

        # ===== 步骤7：补查不在活跃列表中的停滞人员下班卡 =====
        # 先读取需要补查的人员列表（快速读操作）
        conn = get_db()
        c = conn.cursor()
        active_names = {pd.get("handlerName") for pd in active_workers if pd.get("handlerName")}
        c.execute("""
            SELECT wr.name, dac.clockout_time FROM worker_realtime wr
            LEFT JOIN daily_attendance_cache dac 
                ON wr.name = dac.name AND dac.attence_date = ?
            LEFT JOIN worker_info_cache ic 
                ON wr.name = ic.name
            WHERE wr.idle_minutes >= 10
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
        conn.close()
        conn = None

        if stagnant_no_clockout:
            print(f"[INFO] 补查 {len(stagnant_no_clockout)} 名非活跃停滞人员的下班打卡: {stagnant_no_clockout}")
            # 逐人：先做网络请求，再短暂开连接写入
            for sname in stagnant_no_clockout:
                _last_q = _attendance_last_query.get(sname, 0)
                if (time.time() - _last_q) < ATTENDANCE_QUERY_INTERVAL:
                    continue
                _attendance_last_query[sname] = time.time()
                try:
                    # 网络请求（不持有数据库连接）
                    api_att = "/hrm/attendance/r/query"
                    params_att = {
                        "warehouseValidity": "EFFECTIVE",
                        "warehouseId": WAREHOUSE_ID,
                        "labourUserName": sname,
                        "attenceDate": current_month,
                        "pageNo": 1,
                        "pageSize": 200,
                    }
                    res_att = request_with_retry(session, api_att, params_att)
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
                        # 短暂开连接写入
                        conn = get_db()
                        c = conn.cursor()
                        c.execute("""
                            UPDATE daily_attendance_cache 
                            SET clockout_time = ?
                            WHERE name = ? AND attence_date = ?
                        """, (clockout_dt_str, sname, today_date))
                        conn.commit()
                        conn.close()
                        conn = None
                        print(f"  [OK] {sname} 补录下班打卡: {clockout_dt_str}")
                    else:
                        print(f"  [INFO] {sname} 暂无下班打卡记录")
                    time.sleep(0.2)
                except Exception as e:
                    print(f"  [WARN] 补查 {sname} 考勤异常: {e}")

        print(f"[OK] [{actual_now.strftime('%H:%M:%S')}] 数据聚合成功，活跃人员：{len(active_workers)}人")

    except Exception as e:
        print(f"[ERROR] 抓取/处理过程异常: {e}")
        import traceback
        traceback.print_exc()
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
