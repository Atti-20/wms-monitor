# -*- coding: utf-8 -*-
import datetime
import json
import os
import random
import time
from typing import List, Dict, Tuple
import pandas as pd
import requests
from urllib.parse import urlencode
from token_manager import get_access_token, PROXY_URL

# ================= 配置参数 =================
HTTP_TIMEOUT = 15
PAGESIZE = 200
# 统一使用 428 大仓 RDC 视角查询，确保品牌等字段完整
MAIN_WAREHOUSE_ID = "428"
ALLOT_IN_WAREHOUSE_ID = "636"


# ============================================

def get_target_date():
    """根据夜班逻辑，智能获取正确的履约日期"""
    now = datetime.datetime.now()
    if now.hour < 8:
        return now.strftime("%Y-%m-%d")
    else:
        return (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")


# Sheet1 非翻包取消件
NON_TURNOVER_COLUMNS = [
    "履约波次", "包裹号", "站区", "线路", "点位",
    "sku编码", "品牌", "商品名称",
    "打印状态", "包裹状态", "包裹核货状态", "收货扫描次数",
    "规格描述", "确认差异人", "确认差异时间"
]

# Sheet3 全部取消件（完整版）
CANCELLED_COLUMN_MAP = {
    "parcelNo": "包裹号",
    "orderNo": "订单号",
    "fulfillDate": "履约日期",
    "fulfillmentWaveName": "履约波次",
    "goodsOwnerName": "货主名称",
    "goodsOwnerType": "货主类型",
    "stationAreaNo": "站区",
    "stationRouteSeq": "线路",
    "poiSerialCode": "点位",
    "printStatus": "打印状态",
    "parcelStatus": "包裹状态",
    "driverCheckStatus": "包裹核货状态",
    "scansCount": "收货扫描次数",
    "skuCode": "sku编码",
    "skuBrand": "品牌",
    "skuName": "商品名称",
    "skuUnitDesc": "规格描述",
    "producerName": "作业方",
    "bindStatus": "包裹绑重标识",
    "receiveMarkType": "收货标识",
    "replenishmentStatus": "补货状态",
    "replenishmentShortageType": "补货类型",
}

# Sheet2 翻包件
TURNOVER_COLUMN_MAP = {
    "id": "翻包记录ID",
    "fulfillmentWaveName": "履约波次名称",
    "turnOverStatus": "翻包状态",
    "warehouseId": "仓库ID",
    "allotInWarehouseId": "调入仓库ID",
    "appointmentTime": "预约日期",
    "skuCode": "sku编码",
    "skuBrand": "品牌",
    "skuName": "商品名称",
    "cancelParcelStationAreaNo": "取消包裹站区",
    "cancelParcelThreeSegCode": "取消包裹三段码",
    "newParcelStationAreaNo": "新包裹站区",
    "newParcelThreeSegCode": "新包裹三段码",
    "newParcelNo": "新包裹号",
    "cancelParcelNo": "取消包裹号"
}


def _proxy_get(original_url, token):
    """通过 NoCode Proxy 发起 GET 请求"""
    headers = {
        "Origin-Url": original_url,
        "access-token": token,
    }
    try:
        r = requests.get(PROXY_URL, headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code == 401:
            return None
        return r.json()
    except Exception:
        return None


def get_cancelled_parcels(token, date):
    page_no = 1
    all_content = []
    while True:
        params = {
            "warehouseId": MAIN_WAREHOUSE_ID,
            "appointmentTime": date,
            "allotInWarehouseId": ALLOT_IN_WAREHOUSE_ID,
            "skuName": "",
            "skuCodeList": "",
            "parcelNo": "",
            "orderNo": "",
            "stationRouteSeq": "",
            "poiSerialCode": "",
            "productionStatusList": "CANCEL_AFTER_PRODUCED",
            "fulfillmentWaveId": "",
            "isFulfillmentWaveGray": "true",
            "orderBy": "",
            "printStatus": "PRINTED",
            "asc": "true",
            "pageNo": page_no,
            "pageSize": PAGESIZE,
            "wareHouseId": MAIN_WAREHOUSE_ID,
        }
        original_url = f"https://klwms.meituan.com/haina/ojs/rdc/r/parcelList?{urlencode(params, doseq=True)}"
        try:
            j = _proxy_get(original_url, token)
            if not j or j.get("code") != 200:
                break
            content = j["data"].get("pageContent", [])
            if not content:
                break
            all_content.extend(content)
            if len(all_content) >= j["data"]["page"]["totalCount"]:
                break
            page_no += 1
        except:
            break
    return len(all_content), all_content, False


def get_turnover_parcels(token, date):
    page_no = 1
    all_items = []
    while True:
        params = {
            "warehouseId": MAIN_WAREHOUSE_ID,
            "appointmentTime": date,
            "allotInWarehouseId": ALLOT_IN_WAREHOUSE_ID,
            "skuCode": "",
            "skuName": "",
            "newParcelNo": "",
            "cancelParcelNo": "",
            "pageNo": page_no,
            "pageSize": PAGESIZE,
            "wareHouseId": MAIN_WAREHOUSE_ID,
        }
        original_url = f"https://klwms.meituan.com/haina/ojs/rdc/turnover/r/pageList?{urlencode(params, doseq=True)}"
        try:
            j = _proxy_get(original_url, token)
            if not j or j.get("code") != 200:
                break
            items = j["data"].get("pageContent", [])
            if not items:
                break
            all_items.extend(items)
            if len(all_items) >= j["data"]["page"]["totalCount"]:
                break
            page_no += 1
        except:
            break
    nos = [it.get("cancelParcelNo") or it.get("parcelNo") for it in all_items if
           it.get("cancelParcelNo") or it.get("parcelNo")]
    return len(all_items), all_items, nos, False


def get_parcel_log(token, parcel_no):
    params = {
        "parcelNo": parcel_no,
        "allotInWarehouseId": ALLOT_IN_WAREHOUSE_ID,
        "wareHouseId": MAIN_WAREHOUSE_ID,
        "warehouseId": MAIN_WAREHOUSE_ID,
    }
    original_url = f"https://klwms.meituan.com/haina/parcel/r/parcelLog?{urlencode(params, doseq=True)}"
    try:
        j = _proxy_get(original_url, token)
        if not j:
            return "", ""
        logs = j.get("data", {}).get("parcelLogList", [])
        for log in logs:
            if log.get("description") == "包裹确认差异":
                dt = datetime.datetime.fromtimestamp(log["time"] / 1000).strftime('%Y-%m-%d %H:%M:%S')
                return log.get("operatorName", ""), dt
    except:
        pass
    return "", ""


def format_cancelled_data(parcel, turnover_list):
    row = {cn: parcel.get(en, "") for en, cn in CANCELLED_COLUMN_MAP.items()}
    row["收货扫描次数"] = parcel.get("scansCount", 0)
    row["确认差异人"] = parcel.get("确认差异人", "")
    row["确认差异时间"] = parcel.get("确认差异时间", "")

    # 原生接口自带品牌，做个安全兜底即可
    row["品牌"] = parcel.get("skuBrand") or parcel.get("brandName") or ""

    area = parcel.get("stationAreaNo", "")
    point = parcel.get("poiSerialCode", "")
    route = parcel.get("stationRouteSeq", "")
    row["取消包裹三段码"] = f"{area}{point}{route}"
    row["取消包裹站区"] = area

    pno = parcel.get("parcelNo", "")
    turn = next((t for t in turnover_list if t.get("cancelParcelNo") == pno), None)
    if turn:
        row["处理方式"] = turn.get("turnOverStatus", "")
        row["新包裹站区"] = turn.get("stationAreaNo", "")
        new_area = turn.get("stationAreaNo", "")
        new_point = turn.get("poiSerialCode", "")
        new_route = turn.get("stationRouteSeq", "")
        row["新包裹三段码"] = f"{new_area}{new_point}{new_route}"
    else:
        row["处理方式"] = "非翻包件"
        row["新包裹站区"] = ""
        row["新包裹三段码"] = ""
    return row


def format_turnover_data(item):
    row = {cn: item.get(en, "") for en, cn in TURNOVER_COLUMN_MAP.items()}
    row["品牌"] = item.get("skuBrand") or item.get("brandName") or ""
    return row


def safe_reindex_df(df, cols):
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df[cols]


def generate_excel_for_web(token):
    """生成取消件/翻包件 Excel 报表（使用 SSO Token 鉴权）"""
    date = get_target_date()
    excel_file = f"取消件_翻包件汇总_{date}.xlsx"

    total_c, cancels, invalid = get_cancelled_parcels(token, date)
    if invalid:
        raise Exception("Token已失效，请确认MOA登录态")

    total_t, turns, turn_nos, invalid = get_turnover_parcels(token, date)
    if invalid:
        raise Exception("Token已失效，请确认MOA登录态")

    non_list = [p for p in cancels if p.get("parcelNo") not in turn_nos]

    # 获取非翻包件的确认差异人日志
    for p in non_list:
        op, dt = get_parcel_log(token, p.get("parcelNo"))
        p["确认差异人"] = op
        p["确认差异时间"] = dt

    # 数据格式化与装载
    df1 = pd.DataFrame([format_cancelled_data(p, turns) for p in non_list])
    df1 = safe_reindex_df(df1, NON_TURNOVER_COLUMNS)

    df2 = pd.DataFrame([format_turnover_data(it) for it in turns])
    df2 = safe_reindex_df(df2, list(TURNOVER_COLUMN_MAP.values()))

    df3 = pd.DataFrame([format_cancelled_data(p, turns) for p in cancels])
    cancel_full_cols = list(CANCELLED_COLUMN_MAP.values()) + [
        "确认差异人", "确认差异时间", "取消包裹三段码", "取消包裹站区",
        "处理方式", "新包裹站区", "新包裹三段码"
    ]
    df3 = safe_reindex_df(df3, cancel_full_cols)

    df1 = df1.fillna("")
    df2 = df2.fillna("")
    df3 = df3.fillna("")

    with pd.ExcelWriter(excel_file, engine="openpyxl") as w:
        df1.to_excel(w, sheet_name="非翻包取消件", index=False)
        df2.to_excel(w, sheet_name="翻包件", index=False)
        df3.to_excel(w, sheet_name="全部取消件", index=False)

    table_data = df1.to_dict('records')
    return os.path.abspath(excel_file), table_data
