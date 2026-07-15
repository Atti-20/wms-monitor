# -*- coding: utf-8 -*-
"""
定时任务调度模块
负责：activate_sync / unified_sync / 结单总结生成
"""
import threading
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
import database
from spiders.picking import process_and_save
from spiders.abnormal import fetch_abnormal_parcels
from spiders.stagnant import check_picking_stagnant
from token_manager import TOKEN_STATUS, get_access_token
from spiders.base import get_logical_date, DEFAULT_WAREHOUSE_ID

# ===== 统一定时任务：按需启动 =====
_processing_lock = threading.Lock()
_sync_counter = 0
_last_dashboard_access = None
_SYNC_TIMEOUT_MINUTES = 10
_sync_active = False
_auto_mode = False  # 18:00 自动激活模式（不受超时限制，持续到凌晨3点）


def activate_sync():
    """用户访问 dashboard 时调用，激活定时抓取"""
    global _last_dashboard_access, _sync_active
    _last_dashboard_access = datetime.now()
    if not _sync_active:
        _sync_active = True
        print("[数据引擎] 用户进入监控页面，定时抓取已激活")
        threading.Thread(target=unified_sync, daemon=True).start()


def deactivate_sync():
    """用户离开页面时调用，立即停止定时抓取"""
    global _sync_active
    if _sync_active:
        _sync_active = False
        print("[数据引擎] 用户关闭监控页面，定时抓取已立即停止")


def unified_sync():
    """
    统一同步任务，30秒一轮：
    - 每轮：process_and_save + check_picking_stagnant
    - 每10轮（约5分钟）：fetch_abnormal_parcels
    """
    global _sync_counter, _sync_active, _auto_mode
    if not _processing_lock.acquire(blocking=False):
        print("[数据引擎] 上一轮仍在执行，跳过本轮")
        return
    try:
        if _last_dashboard_access is None:
            _sync_active = False
            return

        # 自动模式下：持续运行到凌晨 1:30 后自动停止
        now = datetime.now()
        if _auto_mode:
            # 1:30~17:59 之间停止自动模式
            if (now.hour == 1 and now.minute >= 30) or (2 <= now.hour < 18):
                _sync_active = False
                _auto_mode = False
                print("[数据引擎] 凌晨1:30已过，自动抓取模式结束")
                return
        else:
            # 非自动模式：按原有超时逻辑
            elapsed = (now - _last_dashboard_access).total_seconds() / 60
            if elapsed > _SYNC_TIMEOUT_MINUTES:
                if _sync_active:
                    _sync_active = False
                    print(f"[数据引擎] 超过{_SYNC_TIMEOUT_MINUTES}分钟无人访问监控页面，抓取已暂停")
                return

        if not TOKEN_STATUS.get("ok"):
            token = get_access_token()
            if not token:
                print("[数据引擎] SSO Token 当前不可用，定时抓取任务已挂起。请确认本机 MOA 已登录。")
                return

        _sync_counter += 1
        # 遍历所有仓库采集数据，每个仓库写入独立的数据库文件
        from spiders.base import WAREHOUSES
        for wh_id in WAREHOUSES:
            try:
                process_and_save(warehouse_id=wh_id)
                check_picking_stagnant(warehouse_id=wh_id)
                if _sync_counter % 10 == 0:
                    fetch_abnormal_parcels(warehouse_id=wh_id)
            except Exception as e:
                print(f"[数据引擎] 仓库 {wh_id} 采集异常: {e}")

        # 每轮检查：全部拣货/调拨任务完成时自动停止
        # 条件：pickStatus=picking 为0 且 pickStatus=created 为0 且 调拨单CREATED 为0 且 已拣>0
        try:
            conn = database.get_db(DEFAULT_WAREHOUSE_ID)
            c = conn.cursor()
            c.execute("SELECT value FROM kv_store WHERE key = 'pick_status_picking_count'")
            row = c.fetchone()
            picking_count = int(row[0]) if row else -1
            c.execute("SELECT value FROM kv_store WHERE key = 'pick_status_created_count'")
            row = c.fetchone()
            created_count = int(row[0]) if row else -1
            c.execute("SELECT value FROM kv_store WHERE key = 'allocation_created_count'")
            row = c.fetchone()
            alloc_created = int(row[0]) if row else -1
            c.execute("SELECT value FROM kv_store WHERE key = 'total_all_picked'")
            row_picked = c.fetchone()
            picked = int(row_picked[0]) if row_picked else 0
            conn.close()

            all_done = picking_count == 0 and created_count == 0 and alloc_created == 0 and picked > 0
            if all_done and _auto_mode:
                logical_date = get_logical_date()
                # 全部完成，先生成当日总结
                c2 = database.get_db(DEFAULT_WAREHOUSE_ID).cursor()
                c2.execute('SELECT SUM(total_count) FROM worker_realtime')
                total = c2.fetchone()[0] or 0
                c2.connection.close()
                if total > 0:
                    result = generate_picking_summary(logical_date, total)
                    if result:
                        print(f"[数据引擎] 已自动生成 {logical_date} 拣货总结")
                _sync_active = False
                _auto_mode = False
                print(f"[数据引擎] 全部拣货/调拨任务已完成（已拣:{picked}, 拣货中:0, 已生成:0, 待调拨:0），自动停止抓取")
                return
        except Exception as e:
            print(f"[数据引擎] 检查拣货完成状态异常: {e}")

        # 每10轮检查一次：如果已到凌晨2:00~3:00，自动生成当日拣货总结
        if _sync_counter % 10 == 0:
            now = datetime.now()
            if 2 <= now.hour <= 3:
                logical_date = get_logical_date()
                conn = database.get_db(DEFAULT_WAREHOUSE_ID)
                c = conn.cursor()
                c.execute('SELECT SUM(total_count) FROM worker_realtime')
                total = c.fetchone()[0] or 0
                conn.close()
                if total > 0:
                    result = generate_picking_summary(logical_date, total)
                    if result:
                        print(f"[数据引擎] 已自动生成 {logical_date} 拣货总结")
    finally:
        _processing_lock.release()


def generate_picking_summary(today, total_picked):
    """生成今日拣货总结（基于主仓库数据）"""
    conn = database.get_db(DEFAULT_WAREHOUSE_ID)
    c = conn.cursor()
    c.execute('''
        SELECT name, team_name, is_new_staff, total_count, feeding_area_count, 
               seasoning_area_count, efficiency, idle_minutes 
        FROM worker_realtime
    ''')
    workers = c.fetchall()
    if not workers:
        print(f"[WARN] 生成总结时 worker_realtime 为空，跳过生成")
        conn.close()
        return False
    total_workers = len(workers)

    today_str = today
    c.execute('SELECT COUNT(*) FROM daily_worker_picking_summary WHERE summary_date = ?', (today_str,))
    if c.fetchone()[0] > 0:
        conn.close()
        return True

    rows = []
    for w in workers:
        rows.append((
            today_str, w['name'], w['team_name'], w['is_new_staff'],
            w['total_count'], w['feeding_area_count'], w['seasoning_area_count'],
            w['efficiency'], w['idle_minutes'],
            1 if (w['idle_minutes'] or 0) >= 15 else 0
        ))

    c.executemany('''
        INSERT OR IGNORE INTO daily_worker_picking_summary
        (summary_date, name, team_name, is_new_staff, total_count, feeding_area_count,
         seasoning_area_count, efficiency, idle_minutes, is_over_15min_idle)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', rows)

    c.execute('''
        INSERT OR IGNORE INTO daily_picking_summary
        (summary_date, total_picked_qty, total_online_workers)
        VALUES (?, ?, ?)
    ''', (today_str, total_picked, total_workers))

    conn.commit()
    conn.close()
    return True


# ===== 18:00 自动激活抓取（无需用户打开页面）=====
def _auto_activate_evening():
    """每天 18:00 自动激活数据抓取，确保 sparkline 从开工就有数据"""
    global _last_dashboard_access, _sync_active, _auto_mode
    if _sync_active:
        print("[数据引擎] 18:00 自动激活：抓取已在运行中，跳过")
        return
    # 检查 Token 是否可用
    if not TOKEN_STATUS.get("ok"):
        token = get_access_token()
        if not token:
            print("[数据引擎] 18:00 自动激活失败：SSO Token 不可用，请确认本机 MOA 已登录")
            return
    _last_dashboard_access = datetime.now()
    _sync_active = True
    _auto_mode = True  # 标记为自动模式，不受10分钟超时限制
    print("[数据引擎] 18:00 定时自动激活抓取，sparkline 将从开工起记录（持续到凌晨1:30）")
    threading.Thread(target=unified_sync, daemon=True).start()


# ===== 启动时回填：如果在工作时段内启动且缺少快照，自动回填 =====
def _backfill_today_on_startup():
    """
    Flask 启动时检查：如果当前处于工作时段(18:00~01:30)且今天逻辑日缺少快照，
    通过 API 抓取已完成的拣货记录回填缺失的时段快照。
    """
    now = datetime.now()
    # 判断是否在工作时段内（18:00~01:30）
    in_work_hours = now.hour >= 18 or (now.hour == 0) or (now.hour == 1 and now.minute < 30)
    if not in_work_hours:
        return

    logical_today_dt = now - timedelta(days=1) if now.hour < 8 else now
    logical_date_str = logical_today_dt.strftime("%Y-%m-%d")

    # 检查今天已有多少快照
    conn = database.get_db(DEFAULT_WAREHOUSE_ID)
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM picking_progress_snapshot WHERE logical_date = ?', (logical_date_str,))
    existing_count = c.fetchone()[0]
    conn.close()

    # 计算从 18:00 到现在应该有多少个 10 分钟槽位
    if now.hour >= 18:
        expected_slots = (now.hour - 18) * 6 + now.minute // 10
    else:
        expected_slots = 6 * 6 + now.hour * 6 + now.minute // 10  # 18~24 共36个 + 凌晨部分

    if existing_count >= expected_slots - 1:
        print(f"[数据引擎] 启动回填检查：今日已有 {existing_count} 条快照，无需回填")
        return

    print(f"[数据引擎] 启动回填：今日应有约 {expected_slots} 条快照，实际仅 {existing_count} 条，开始回填...")

    # 使用 _backfill_snapshots 中的逻辑回填今天
    try:
        from _backfill_snapshots import fetch_picked_records, build_snapshots_by_logical_date
        from spiders.base import get_shared_session

        session = get_shared_session()
        records = fetch_picked_records(session, logical_date_str)
        if not records:
            print("[数据引擎] 启动回填：未获取到已完成记录，跳过")
            return

        snapshots_by_date = build_snapshots_by_logical_date(records)
        if logical_date_str not in snapshots_by_date:
            print(f"[数据引擎] 启动回填：无属于 {logical_date_str} 的快照数据")
            return

        snapshots = snapshots_by_date[logical_date_str]
        # 只回填 18:00~01:20 范围内的时段，且不覆盖已有数据
        conn = database.get_db(DEFAULT_WAREHOUSE_ID)
        c = conn.cursor()
        filled_count = 0
        for slot, picked in snapshots.items():
            # 检查是否在工作时段范围内
            h, m = int(slot[:2]), int(slot[3:])
            if h >= 18 or h == 0 or (h == 1 and m <= 20):
                # 不覆盖已有的实时数据
                c.execute('SELECT COUNT(*) FROM picking_progress_snapshot WHERE logical_date = ? AND time_slot = ?',
                          (logical_date_str, slot))
                if c.fetchone()[0] == 0:
                    c.execute('''INSERT INTO picking_progress_snapshot
                                 (logical_date, time_slot, total_picked, total_unpicked, snapshot_time)
                                 VALUES (?, ?, ?, 0, ?)''',
                              (logical_date_str, slot, picked, f"{logical_date_str} {slot}:00"))
                    filled_count += 1
        conn.commit()
        conn.close()
        print(f"[数据引擎] 启动回填完成：补充了 {filled_count} 个时段快照")
    except Exception as e:
        print(f"[数据引擎] 启动回填异常: {e}")


# ===== 启动时自动激活（如果在工作时段内）=====
def _startup_auto_activate():
    """Flask 启动时，如果在工作时段(18:00~01:30)内，自动激活抓取"""
    global _last_dashboard_access, _sync_active, _auto_mode
    now = datetime.now()
    in_work_hours = now.hour >= 18 or (now.hour == 0) or (now.hour == 1 and now.minute < 30)
    if not in_work_hours:
        return
    if _sync_active:
        return
    # 检查 Token
    if not TOKEN_STATUS.get("ok"):
        token = get_access_token()
        if not token:
            print("[数据引擎] 启动自动激活失败：SSO Token 不可用")
            return
    _last_dashboard_access = datetime.now()
    _sync_active = True
    _auto_mode = True
    print(f"[数据引擎] 启动时处于工作时段({now.strftime('%H:%M')})，自动激活抓取")
    threading.Thread(target=unified_sync, daemon=True).start()


# ===== 每天 19:05 抓取销售预测 =====
def _fetch_daily_forecast():
    """定时抓取所有仓库今日销售预测值"""
    from spiders.forecast import fetch_forecast
    from spiders.base import WAREHOUSES
    for wh_id in WAREHOUSES:
        try:
            result = fetch_forecast(warehouse_id=wh_id)
            if result:
                print(f"[数据引擎] 仓库{wh_id}销售预测抓取成功: {result} 件")
            else:
                print(f"[数据引擎] 仓库{wh_id}销售预测抓取失败")
        except Exception as e:
            print(f"[数据引擎] 仓库{wh_id}销售预测抓取异常: {e}")


def _check_and_fetch_forecast():
    """
    每 30 分钟自动检测：数据库中是否有次日预测数据。
    若没有则触发抓取，直到写入成功后不再重复抓取。
    适用于服务在 19:05 之后才启动的场景。
    注意：00:00~19:05 期间不执行（BI 看板数据尚未更新）。
    """
    now = datetime.now()
    # 00:00~19:05 期间跳过（看板数据尚未更新为次日预测）
    if now.hour < 19 or (now.hour == 19 and now.minute < 5):
        return
    from spiders.forecast import get_tomorrow_forecast, fetch_forecast
    from spiders.base import WAREHOUSES
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    for wh_id in WAREHOUSES:
        existing = get_tomorrow_forecast(warehouse_id=wh_id)
        if existing is not None:
            print(f"[数据引擎] 仓库{wh_id}次日({tomorrow})预测已存在({existing}件)，跳过")
            continue
        print(f"[数据引擎] 仓库{wh_id}次日({tomorrow})预测缺失，触发抓取...")
        try:
            result = fetch_forecast(warehouse_id=wh_id)
            if result:
                print(f"[数据引擎] 仓库{wh_id}自动抓取成功: {tomorrow} = {result} 件")
            else:
                print(f"[数据引擎] 仓库{wh_id}自动抓取失败，30分钟后重试")
        except Exception as e:
            print(f"[数据引擎] 仓库{wh_id}自动抓取异常: {e}")


# ===== 每天 18:30 自动检查拣货人员权限 =====
def _auto_check_permissions():
    """每天开工后半小时自动检查拣货人员权限，补开 GXJHSM"""
    try:
        from spiders.permission import check_and_fix_permissions
        print("[数据引擎] 18:30 定时权限检查启动...")
        result = check_and_fix_permissions()
        if result:
            fixed = result.get("fixed", 0)
            total = result.get("checked", 0)
            if fixed > 0:
                print(f"[数据引擎] 权限检查完成：共检查 {total} 人，补开 {fixed} 人的 GXJHSM 权限")
            else:
                print(f"[数据引擎] 权限检查完成：共检查 {total} 人，全部权限正常")
    except Exception as e:
        print(f"[数据引擎] 权限检查异常: {e}")


# 启动调度器
scheduler = BackgroundScheduler()
scheduler.add_job(func=unified_sync, trigger="interval", seconds=30)
# 每天 18:00 自动激活抓取（即使没人打开页面）
scheduler.add_job(func=_auto_activate_evening, trigger="cron", hour=18, minute=0)
# 每天 18:30 自动检查拣货人员权限并补开 GXJHSM
scheduler.add_job(func=_auto_check_permissions, trigger="cron", hour=18, minute=30)
# 每天 19:05 抓取销售预测（看板数据更新时间）
scheduler.add_job(func=_fetch_daily_forecast, trigger="cron", hour=19, minute=5)
# 每 30 分钟检测次日预测数据是否已入库，缺失则自动抓取（覆盖服务晚启动的场景）
scheduler.add_job(func=_check_and_fetch_forecast, trigger="interval", minutes=30)
scheduler.start()

# 启动时：回填缺失快照 + 自动激活抓取
threading.Thread(target=_backfill_today_on_startup, daemon=True).start()
_startup_auto_activate()

print("[数据引擎] 就绪，每天18:00自动激活抓取 / 工作时段内启动自动回填+激活")
