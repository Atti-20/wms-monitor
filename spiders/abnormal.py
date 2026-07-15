# -*- coding: utf-8 -*-
"""
异常包裹巡检模块
对应原 WMSDataSpider.fetch_abnormal_parcels()
"""
from datetime import datetime, timedelta
from database import get_db
from spiders.base import request_with_retry, get_warehouse_id, get_shared_session


def fetch_abnormal_parcels(warehouse_id=None):
    """抓取并分析包裹数量异常明细 (本地缓存防重 + 逻辑熔断)"""
    if warehouse_id is None:
        warehouse_id = get_warehouse_id()
    _wh_id = str(warehouse_id)
    session = get_shared_session()
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] [INFO] 开始智能巡检异常包裹...")

    target_warehouses = [349, 479, 501, 557, 636, 100, 189, 546, 616]

    actual_now = datetime.now()
    logical_today = actual_now - timedelta(days=1) if actual_now.hour < 8 else actual_now
    logical_tomorrow = logical_today + timedelta(days=1)
    appointment_date = logical_tomorrow.strftime("%Y-%m-%d")

    start_time = f"{logical_today.strftime('%Y-%m-%d')} 06:00:00"
    end_time = f"{logical_tomorrow.strftime('%Y-%m-%d')} 12:00:00"

    conn = get_db(_wh_id)
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
            api_parcel_list = "/haina/ojs/rdc/r/containerParcelPrintList"
            params1 = {
                "warehouseId": _wh_id,
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
                "wareHouseId": _wh_id,
            }

            res1 = request_with_retry(session, api_parcel_list, params1)
            if not res1 or not res1.get("data"):
                continue

            page_content1 = res1["data"].get("pageContent", [])

            for task in page_content1:
                parcel_qty = task.get("parcelQty")
                sku_qty = task.get("skuQty")

                if not parcel_qty or sku_qty == parcel_qty:
                    continue

                task_no = task.get("containerPrintTaskNo")
                zone_pick_bill_no = task.get("zonePickBillNo")

                if task_no in existing_tasks:
                    continue

                # 查拣货单确认是否已拣完
                api_pick = "/haina/outbound/zonepick/r/pageList"
                params3 = {
                    "createTimeStart": start_time,
                    "createTimeEnd": end_time,
                    "zonePickBillNo": zone_pick_bill_no,
                    "billType": "DB",
                    "deliveryRegionIds": "",
                    "pageNo": 1,
                    "pageSize": 20,
                    "wareHouseId": _wh_id,
                    "warehouseId": _wh_id,
                }

                res3 = request_with_retry(session, api_pick, params3)
                if not res3 or not res3.get("data"):
                    continue

                records3 = res3["data"].get("pageContent", [])
                if not records3:
                    continue

                rec = records3[0]
                if rec.get("pickStatus") != "picked":
                    continue

                # 抓取明细
                api_detail = "/haina/ojs/rdc/r/containerParcelPrintDetail"
                params2 = {
                    "allotInWarehouseId": wh_id,
                    "containerPrintTaskNo": task_no,
                    "warehouseId": _wh_id,
                    "wareHouseId": _wh_id,
                }

                res2 = request_with_retry(session, api_detail, params2)
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
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()
