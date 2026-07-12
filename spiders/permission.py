# -*- coding: utf-8 -*-
"""
拣货人员权限检查与自动补开模块
功能：
1. 从 worker_realtime 表获取当前活跃拣货人员名单
2. 通过 pageUserList 获取 userId / providerId
3. 通过 queryUserDetail 检查每人权限
4. 对缺少 GXJHSM（共享拣货扫码）的人员自动补开
"""
import time
import json
from datetime import datetime
from database import get_db
from spiders.base import (
    request_with_retry, get_shared_session, WAREHOUSE_ID, BASE_URL
)

# 权限码定义
GXJHSM = "GXJHSM"          # 共享拣货扫码
RFCKCZ = "RFCKCZ"           # RF 操作员（仓库操作）

# 劳务公司 providerId → orgId/orgName 映射（permissionAllot 需要）
PROVIDER_ORG_MAP = {
    2451: {"orgId": 40516, "orgName": "厦门今元人才科技有限公司"},
    2233: {"orgId": 30144, "orgName": "深圳市萍聚四海劳务派遣有限公司"},
    1779: {"orgId": 25875, "orgName": "深圳市微科林人力资源有限公司"},
    2339: {"orgId": 33968, "orgName": "深圳市百仕达劳务派遣有限公司"},
    2322: {"orgId": 33776, "orgName": "深圳市中信科劳务派遣有限公司"},
    2273: {"orgId": 31227, "orgName": "深圳盛世明德实业集团有限公司"},
    2203: {"orgId": 29795, "orgName": "深圳市东联人力资源有限公司"},
}

# 尝试 providerId 的顺序（2451 最常见排第一）
PROVIDER_TRY_ORDER = [2451, 2233, 1779, 2339, 2322, 2273, 2203]


def _resolve_user_ids(session, name):
    """
    通过 pageUserList 查找员工的 userId 和 providerId。
    返回 (userId, providerId) 或 (None, None)。
    """
    api = "/hrm/labour/inhouse/user/r/pageUserList"
    params = {
        "name": name,
        "warehouseValidity": "EFFECTIVE",
        "warehouseIdList": WAREHOUSE_ID,
        "jobStatus": "INCUMBENCY",
        "pageNo": 1,
        "pageSize": 20,
    }
    res = request_with_retry(session, api, params)
    if not res:
        return None, None

    page_content = res.get("data", {}).get("pageContent", [])
    for item in page_content:
        if item.get("name") == name:
            user_id = item.get("userId")
            provider_id = item.get("providerId")
            return user_id, provider_id

    return None, None


def _query_user_permissions(session, user_id, provider_id):
    """
    通过 queryUserDetail 查询用户已有权限列表。
    返回 spUserOrgNodeVos 列表（原始数据），或 None 表示失败。
    """
    api = f"/hrm/labour/inhouse/user/r/queryUserDetail"
    params = {
        "userId": user_id,
        "providerId": provider_id,
    }
    res = request_with_retry(session, api, params)
    if not res:
        return None

    data = res.get("data", {})
    if not data:
        return None

    return data.get("spUserOrgNodeVos", [])


def _has_permission(org_vos, pos_code):
    """检查 spUserOrgNodeVos 中是否已包含指定权限码"""
    if not org_vos:
        return False
    for node in org_vos:
        if node.get("posCode") == pos_code:
            return True
    return False


def _allot_permission(session, user_id, provider_id, existing_vos, add_pos_codes, user_name=""):
    """
    通过 permissionAllot 接口分配权限。
    注意：此接口为全量覆盖语义，必须把现有权限 + 新增权限一起提交。
    接口要求 name 字段必填，mobile 可为空。
    """
    org_info = PROVIDER_ORG_MAP.get(provider_id)
    if not org_info:
        return False, f"未知的 providerId: {provider_id}，无法获取 orgId/orgName"

    # 权限码 → posName 映射
    POS_NAME_MAP = {
        "RFCKCZ": "RF操作员",
        "GXJHSM": "共享拣货扫码",
        "RFQZQ": "RF前置区",
        "CCBZZ": "仓储包装组",
    }

    # 构建完整的权限列表（现有 + 新增）
    final_vos = list(existing_vos) if existing_vos else []
    existing_codes = {v.get("posCode") for v in final_vos}

    for code in add_pos_codes:
        if code not in existing_codes:
            final_vos.append({
                "posCode": code,
                "posName": POS_NAME_MAP.get(code, code),
                "orgId": org_info["orgId"],
                "orgName": org_info["orgName"],
            })

    # 构建请求体（name 和 mobile 是必填字段）
    payload = {
        "userId": user_id,
        "name": user_name,
        "mobile": "",
        "providerId": provider_id,
        "spUserOrgNodeVos": final_vos,
    }

    url = f"{BASE_URL}/hrm/labour/inhouse/user/w/permissionAllot"

    try:
        # permissionAllot 是 POST 接口，需要通过 Cookie 或 Proxy 直接发 POST
        # 这里复用 session 的鉴权信息
        from token_manager import get_access_token, PROXY_URL
        token = get_access_token()

        if token:
            # 通过 NoCode Proxy 发 POST
            headers = {
                "Origin-Url": url,
                "access-token": token,
                "Content-Type": "application/json",
            }
            response = session.post(PROXY_URL, headers=headers, json=payload, timeout=15)
        else:
            # Cookie 直连模式
            from spiders.base import get_user_cookie
            cookie_str = get_user_cookie()
            if not cookie_str:
                return False, "无可用的 Token 或 Cookie"
            headers = {
                "Cookie": cookie_str,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0",
            }
            response = session.post(url, headers=headers, json=payload, timeout=15)

        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"

        res_data = response.json()
        code = res_data.get("code")
        if code in (0, 200) or res_data.get("success") or res_data.get("status") == 1:
            return True, "OK"
        else:
            return False, res_data.get("message") or res_data.get("msg") or str(res_data)

    except Exception as e:
        return False, str(e)


def check_and_fix_permissions():
    """
    核心流程：检查所有拣货人员权限，自动补开 GXJHSM。
    返回结果字典：
    {
        "check_time": "2026-06-21 20:30:00",
        "total_workers": 50,
        "checked": 45,
        "already_ok": 40,
        "fixed": 3,
        "fix_failed": 1,
        "skipped": 5,
        "details": [...]
    }
    """
    session = get_shared_session()
    check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[权限检查] [{check_time}] 开始检查拣货人员权限...")

    # 1. 从 worker_realtime 获取当前活跃拣货人员（排除标记为非拣货的人员）
    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT w.name FROM worker_realtime w
        LEFT JOIN worker_info_cache ic ON w.name = ic.name
        WHERE w.total_count > 0 AND COALESCE(ic.is_excluded, 0) = 0
    ''')
    active_workers = [row["name"] for row in c.fetchall()]
    conn.close()

    if not active_workers:
        print("[权限检查] 当前无活跃拣货人员，跳过")
        return {
            "check_time": check_time,
            "total_workers": 0,
            "checked": 0,
            "already_ok": 0,
            "fixed": 0,
            "fix_failed": 0,
            "skipped": 0,
            "details": [],
        }

    print(f"[权限检查] 共 {len(active_workers)} 名活跃拣货人员")

    results = {
        "check_time": check_time,
        "total_workers": len(active_workers),
        "checked": 0,
        "already_ok": 0,
        "fixed": 0,
        "fix_failed": 0,
        "skipped": 0,
        "details": [],
    }

    for name in active_workers:
        detail = {"name": name, "status": "", "message": ""}
        try:
            # 2. 解析 userId / providerId
            user_id, provider_id = _resolve_user_ids(session, name)
            if not user_id or not provider_id:
                detail["status"] = "skipped"
                detail["message"] = "无法通过 pageUserList 找到该员工"
                results["skipped"] += 1
                results["details"].append(detail)
                print(f"  [跳过] {name}: 无法找到 userId/providerId")
                time.sleep(0.15)
                continue

            detail["userId"] = user_id
            detail["providerId"] = provider_id

            # 3. 查询当前权限
            org_vos = _query_user_permissions(session, user_id, provider_id)
            if org_vos is None:
                detail["status"] = "skipped"
                detail["message"] = "queryUserDetail 查询失败"
                results["skipped"] += 1
                results["details"].append(detail)
                print(f"  [跳过] {name}: 权限查询失败")
                time.sleep(0.15)
                continue

            results["checked"] += 1
            existing_codes = [v.get("posCode") for v in org_vos]
            detail["existing_permissions"] = existing_codes

            # 4. 检查是否有 GXJHSM
            if _has_permission(org_vos, GXJHSM):
                detail["status"] = "ok"
                detail["message"] = "已有 GXJHSM 权限"
                results["already_ok"] += 1
                print(f"  [OK] {name}: 已有 GXJHSM")
            else:
                # 5. 自动补开 GXJHSM
                print(f"  [修复] {name}: 缺少 GXJHSM，正在补开...")
                success, msg = _allot_permission(
                    session, user_id, provider_id, org_vos, [GXJHSM], user_name=name
                )
                if success:
                    detail["status"] = "fixed"
                    detail["message"] = "已成功补开 GXJHSM"
                    results["fixed"] += 1
                    print(f"  [已修复] {name}: GXJHSM 补开成功")
                else:
                    detail["status"] = "fix_failed"
                    detail["message"] = f"补开失败: {msg}"
                    results["fix_failed"] += 1
                    print(f"  [失败] {name}: GXJHSM 补开失败 - {msg}")

            time.sleep(0.2)  # 限流

        except Exception as e:
            detail["status"] = "error"
            detail["message"] = str(e)
            results["skipped"] += 1
            print(f"  [异常] {name}: {e}")

        results["details"].append(detail)

    print(f"[权限检查] 完成: 检查{results['checked']}人, "
          f"正常{results['already_ok']}, 补开{results['fixed']}, "
          f"失败{results['fix_failed']}, 跳过{results['skipped']}")

    # 6. 将结果保存到数据库
    _save_check_result(results)

    return results


def _save_check_result(results):
    """将权限检查结果保存到 kv_store"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO kv_store (key, value) VALUES (?, ?)",
            ("last_permission_check", json.dumps(results, ensure_ascii=False))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[权限检查] 保存结果失败: {e}")


def get_last_check_result():
    """读取上次权限检查结果"""
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT value FROM kv_store WHERE key = 'last_permission_check'")
        row = c.fetchone()
        conn.close()
        if row:
            return json.loads(row["value"])
    except Exception:
        pass
    return None
