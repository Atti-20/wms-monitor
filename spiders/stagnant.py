# -*- coding: utf-8 -*-
"""
拣货卡单监控模块
对应原 WMSDataSpider.check_picking_stagnant()
"""
from datetime import datetime, timedelta
from database import get_db
from spiders.base import (
    request_with_retry, parse_ms_timestamp, WAREHOUSE_ID, get_shared_session
)

# 卡单判定阈值（分钟）
STAGNANT_THRESHOLD_MINUTES = 3


def check_picking_stagnant():
    """
    拣货卡单监控，检测两种异常：
    1. 「拣完未提交」：实拣 >= 应拣，但 pickStatus 超过 3 分钟仍未变为 picked
    2. 「未完成停滞」：实拣 < 应拣，但 lastModifyTime 超过 3 分钟未更新
    """
    session = get_shared_session()
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

    res = request_with_retry(session, api_pick, params)
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
        last_modify = parse_ms_timestamp(last_modify_ts)
        if not last_modify:
            continue

        # 过滤：实拣=0 且领取时间距今不超过5分钟
        accept_ts = rec.get("acceptTime")
        accept_time = parse_ms_timestamp(accept_ts)
        if actual_unit_qty == 0 and accept_time:
            accept_age_mins = (actual_now - accept_time).total_seconds() / 60
            if accept_age_mins < 5:
                continue

        stagnant_mins = (actual_now - last_modify).total_seconds() / 60
        if stagnant_mins < STAGNANT_THRESHOLD_MINUTES:
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
                    SET stagnant_minutes = ?,
                        actual_unit_qty = ?, alert_reason = ?,
                        last_modify_time = ?
                    WHERE id = ?
                ''', (item["stagnant_minutes"],
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

        c.execute('SELECT id, zone_pick_bill_no, record_time FROM picking_stagnant_log WHERE is_resolved = 0')
        all_unresolved = c.fetchall()

        resolve_now_str = actual_now.strftime("%Y-%m-%d %H:%M:%S")
        for row in all_unresolved:
            if row["zone_pick_bill_no"] not in still_stagnant_bills:
                # 计算停滞持续时长 = resolve_time - record_time
                duration_minutes = 0
                if row["record_time"]:
                    try:
                        first_detected = datetime.strptime(row["record_time"], "%Y-%m-%d %H:%M:%S")
                        duration_minutes = round((actual_now - first_detected).total_seconds() / 60, 1)
                    except (ValueError, TypeError):
                        pass
                c.execute('''
                    UPDATE picking_stagnant_log
                    SET is_resolved = 1, resolve_time = ?, stagnant_duration_minutes = ?
                    WHERE id = ?
                ''', (resolve_now_str, duration_minutes, row["id"]))

        resolved_count = sum(1 for row in all_unresolved if row["zone_pick_bill_no"] not in still_stagnant_bills)
        if resolved_count:
            print(f"自动解除 {resolved_count} 条不再卡单的记录")

        conn.commit()
        print(f"拣货卡单写入完成，共 {len(stagnant_list)} 条")
    except Exception as e:
        print(f"拣货卡单写入失败: {e}")
        conn.rollback()
    finally:
        conn.close()
