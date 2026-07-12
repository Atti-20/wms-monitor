# -*- coding: utf-8 -*-
import sqlite3
import os
import time

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wms_monitor.db')

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=20.0)
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.row_factory = sqlite3.Row
    return conn


def execute_with_retry(func, max_retries=3, retry_delay=1.0):
    """
    带重试的数据库操作包装器。
    当遇到 database is locked 时，自动重试指定次数。
    func: 接收 (conn, cursor) 参数的可调用对象，返回结果
    """
    last_err = None
    for attempt in range(max_retries):
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            result = func(conn, c)
            conn.commit()
            return result
        except sqlite3.OperationalError as e:
            last_err = e
            if 'database is locked' in str(e):
                print(f"[DB] database is locked，第 {attempt+1}/{max_retries} 次重试...")
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                time.sleep(retry_delay * (attempt + 1))
            else:
                raise
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    raise last_err

def _safe_alter(c, conn, sql, success_msg=None):
    """安全执行 ALTER TABLE，忽略字段已存在的错误"""
    try:
        c.execute(sql)
        conn.commit()
        if success_msg:
            print(f"✅ {success_msg}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e):
            print(f"⚠️ 字段迁移异常: {e}")

def init_db():
    conn = get_db()
    c = conn.cursor()

    # 1. 员工实时状态表
    c.execute('''
        CREATE TABLE IF NOT EXISTS worker_realtime (
            name TEXT PRIMARY KEY,
            team_name TEXT,
            job_type TEXT,
            is_new_staff BOOLEAN,
            is_temp_worker BOOLEAN DEFAULT 0,
            clockin_time DATETIME,
            last_modify_time DATETIME,
            total_count INTEGER,
            feeding_area_count INTEGER,
            seasoning_area_count INTEGER,
            efficiency REAL,
            idle_minutes REAL,
            is_stagnant BOOLEAN,
            update_time DATETIME,
            max_idle_minutes REAL DEFAULT 0,
            stagnant_10min_count INTEGER DEFAULT 0,
            last_counted_idle_ts TEXT,
            seasoning_bill_count INTEGER DEFAULT 0,
            feeding_bill_count INTEGER DEFAULT 0
        )
    ''')
    # 兼容旧库：如果表已存在但缺少新字段，自动添加
    try:
        c.execute("ALTER TABLE worker_realtime ADD COLUMN seasoning_bill_count INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE worker_realtime ADD COLUMN feeding_bill_count INTEGER DEFAULT 0")
    except Exception:
        pass

    # 2. 各仓实时总件量表（旧，保留兼容）
    c.execute('''
        CREATE TABLE IF NOT EXISTS global_warehouse_realtime (
            warehouse_name TEXT PRIMARY KEY,
            volume INTEGER,
            picked_volume INTEGER DEFAULT 0,
            need_allot_total_count INTEGER DEFAULT 0,
            seasoning_bill_count INTEGER DEFAULT 0,
            feeding_bill_count INTEGER DEFAULT 0
        )
    ''')
    # 兼容旧表字段迁移
    _safe_alter(c, conn, 'ALTER TABLE global_warehouse_realtime ADD COLUMN need_allot_total_count INTEGER DEFAULT 0')
    _safe_alter(c, conn, 'ALTER TABLE global_warehouse_realtime ADD COLUMN seasoning_bill_count INTEGER DEFAULT 0')
    _safe_alter(c, conn, 'ALTER TABLE global_warehouse_realtime ADD COLUMN feeding_bill_count INTEGER DEFAULT 0')

    # 3. 员工档案缓存表
    c.execute('''
        CREATE TABLE IF NOT EXISTS worker_info_cache (
            name TEXT PRIMARY KEY,
            team_name TEXT,
            job_type TEXT,
            entry_time DATETIME,
            is_new_staff BOOLEAN,
            is_temp_worker BOOLEAN DEFAULT 0,
            is_excluded BOOLEAN DEFAULT 0
        )
    ''')
    _safe_alter(c, conn, 'ALTER TABLE worker_info_cache ADD COLUMN is_temp_worker BOOLEAN DEFAULT 0')
    _safe_alter(c, conn, 'ALTER TABLE worker_info_cache ADD COLUMN is_excluded BOOLEAN DEFAULT 0')

    # 4. 每日考勤缓存表
    c.execute('''
        CREATE TABLE IF NOT EXISTS daily_attendance_cache (
            name TEXT,
            attence_date TEXT,
            clockin_time DATETIME,
            PRIMARY KEY (name, attence_date)
        )
    ''')
    _safe_alter(c, conn, 'ALTER TABLE daily_attendance_cache ADD COLUMN clockout_time DATETIME')

    # 5. 人员每日拣货明细总结表
    c.execute('''
        CREATE TABLE IF NOT EXISTS daily_worker_picking_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary_date TEXT,
            name TEXT,
            team_name TEXT,
            is_new_staff BOOLEAN,
            total_count INTEGER,
            feeding_area_count INTEGER,
            seasoning_area_count INTEGER,
            efficiency REAL,
            idle_minutes REAL,
            is_over_15min_idle BOOLEAN,
            UNIQUE(summary_date, name)
        )
    ''')

    # 6. 整体总结表
    c.execute('''
        CREATE TABLE IF NOT EXISTS daily_picking_summary (
            summary_date TEXT PRIMARY KEY,
            total_online_workers INTEGER,
            avg_efficiency REAL,
            stagnant_15min_count INTEGER,
            complete_time DATETIME,
            total_picked_qty INTEGER
        )
    ''')

    # 7. 员工-仓库拣货单数统计表（旧，保留兼容）
    c.execute('''
        CREATE TABLE IF NOT EXISTS worker_warehouse_bill_count (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            work_date TEXT,
            warehouse_name TEXT,
            bill_count INTEGER,
            UNIQUE(name, work_date, warehouse_name)
        )
    ''')

    # 8. 各仓按履约产品的实时数据表
    c.execute('''
        CREATE TABLE IF NOT EXISTS global_warehouse_realtime_wave (
            warehouse_name TEXT,
            fulfillment_wave INTEGER,   -- 1:上午达, 2:下午达, 5:凌晨达
            volume INTEGER DEFAULT 0,
            picked_volume INTEGER DEFAULT 0,
            need_allot_total_count INTEGER DEFAULT 0,
            seasoning_bill_count INTEGER DEFAULT 0,
            feeding_bill_count INTEGER DEFAULT 0,
            PRIMARY KEY (warehouse_name, fulfillment_wave)
        )
    ''')

    # 9. 员工-仓库-履约产品拣货单数统计表
    c.execute('''
        CREATE TABLE IF NOT EXISTS worker_warehouse_wave_bill_count (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            work_date TEXT,
            warehouse_name TEXT,
            fulfillment_wave INTEGER,
            bill_count INTEGER,
            UNIQUE(name, work_date, warehouse_name, fulfillment_wave)
        )
    ''')

    # 10. 异常包裹明细表
    c.execute('''
        CREATE TABLE IF NOT EXISTS abnormal_parcels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_date TEXT,
            container_task_no TEXT,
            zone_pick_bill_no TEXT,
            sku_name TEXT,
            qty_diff INTEGER,
            biz_bill_name TEXT,
            handler_name TEXT,
            fulfillment_wave_name TEXT,
            actual_sku_total_qty INTEGER,
            actual_unit_total_qty INTEGER,
            allot_production_mode INTEGER,
            receptacle_code TEXT,
            create_time DATETIME,
            is_processed BOOLEAN DEFAULT 0,
            sku_pick_qty INTEGER DEFAULT 0,
            sku_parcel_qty INTEGER DEFAULT 0,
            sku_code TEXT,
            sku_brand TEXT,
            UNIQUE(container_task_no, sku_name)
        )
    ''')
    # 兼容旧表字段迁移（逐个安全添加）
    _safe_alter(c, conn, 'ALTER TABLE abnormal_parcels ADD COLUMN is_processed BOOLEAN DEFAULT 0')
    _safe_alter(c, conn, 'ALTER TABLE abnormal_parcels ADD COLUMN sku_pick_qty INTEGER DEFAULT 0')
    _safe_alter(c, conn, 'ALTER TABLE abnormal_parcels ADD COLUMN sku_parcel_qty INTEGER DEFAULT 0')
    _safe_alter(c, conn, 'ALTER TABLE abnormal_parcels ADD COLUMN sku_code TEXT')
    _safe_alter(c, conn, 'ALTER TABLE abnormal_parcels ADD COLUMN sku_brand TEXT')
    _safe_alter(c, conn, 'ALTER TABLE abnormal_parcels ADD COLUMN allot_in_warehouse_id INTEGER')

    # 兼容旧版 worker_realtime 表字段迁移
    _safe_alter(c, conn, 'ALTER TABLE worker_realtime ADD COLUMN max_idle_minutes REAL DEFAULT 0')
    _safe_alter(c, conn, 'ALTER TABLE worker_realtime ADD COLUMN stagnant_10min_count INTEGER DEFAULT 0')
    _safe_alter(c, conn, 'ALTER TABLE worker_realtime ADD COLUMN last_counted_idle_ts TEXT')
    _safe_alter(c, conn, 'ALTER TABLE worker_realtime ADD COLUMN is_temp_worker BOOLEAN DEFAULT 0')

    # 11. 拣货卡单监控表（新增）
    # 记录：实拣数量 = 应拣数量，但 pickStatus 长时间未变为 picked 的人员
    # 或：实拣数量 < 应拣数量，但 lastModifyTime 超过阈值未更新的人员
    c.execute('''
        CREATE TABLE IF NOT EXISTS picking_stagnant_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_time DATETIME,           -- 检测时间
            handler_name TEXT,              -- 拣货人
            zone_pick_bill_no TEXT,         -- 拣货单号
            logic_area_name TEXT,           -- 逻辑区
            biz_bill_name TEXT,             -- 目标仓
            order_unit_qty INTEGER,         -- 应拣数量
            actual_unit_qty INTEGER,        -- 实拣数量
            pick_status TEXT,               -- 当前状态
            last_modify_time DATETIME,      -- 最后更新时间
            stagnant_minutes REAL,          -- 卡单时长(分钟)
            alert_reason TEXT,              -- 告警原因：拣完未提交 / 未完成停滞
            is_resolved BOOLEAN DEFAULT 0,  -- 是否已解除
            resolve_time DATETIME           -- 解除时间
        )
    ''')
    _safe_alter(c, conn, "ALTER TABLE picking_stagnant_log ADD COLUMN alert_reason TEXT")
    _safe_alter(c, conn, "ALTER TABLE picking_stagnant_log ADD COLUMN stagnant_duration_minutes REAL DEFAULT 0")

    # 12. 简单 KV 配置表
    c.execute('''
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # 13. 拣货进度快照表（10分钟粒度，用于实时同时段对比）
    c.execute('''
        CREATE TABLE IF NOT EXISTS picking_progress_snapshot (
            logical_date TEXT,
            time_slot TEXT,
            total_picked INTEGER DEFAULT 0,
            total_unpicked INTEGER DEFAULT 0,
            snapshot_time DATETIME,
            PRIMARY KEY (logical_date, time_slot)
        )
    ''')

    # 13b. 按波次拆分的拣货进度快照表（凌晨达/上午达/下午达等）
    c.execute('''
        CREATE TABLE IF NOT EXISTS picking_progress_snapshot_wave (
            logical_date TEXT,
            time_slot TEXT,
            wave_name TEXT,
            total_picked INTEGER DEFAULT 0,
            snapshot_time DATETIME,
            PRIMARY KEY (logical_date, time_slot, wave_name)
        )
    ''')

    # 14. 班组出勤统计表（按日期+时段+班组统计出勤人数）
    c.execute('''
        CREATE TABLE IF NOT EXISTS team_attendance_hourly (
            attendance_date TEXT,
            hour_slot INTEGER,
            team_name TEXT,
            head_count INTEGER DEFAULT 0,
            PRIMARY KEY (attendance_date, hour_slot, team_name)
        )
    ''')

    # 15. 人员-班组自定义配置表（优先于 worker_info_cache 的班组字段）
    c.execute('''
        CREATE TABLE IF NOT EXISTS team_member_config (
            name TEXT PRIMARY KEY,
            team_name TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 16. 出勤甘特图明细表（每人每天的打卡-下班时段）
    c.execute('''
        CREATE TABLE IF NOT EXISTS attendance_gantt_detail (
            attendance_date TEXT,
            name TEXT,
            team_name TEXT,
            start_time TEXT,
            end_time TEXT,
            PRIMARY KEY (attendance_date, name)
        )
    ''')

    conn.commit()
    conn.close()

if not os.path.exists(DB_FILE):
    init_db()
else:
    # 确保新增表在已有数据库中存在
    _conn = get_db()
    _conn.execute('''
        CREATE TABLE IF NOT EXISTS picking_progress_snapshot (
            logical_date TEXT,
            time_slot TEXT,
            total_picked INTEGER DEFAULT 0,
            total_unpicked INTEGER DEFAULT 0,
            snapshot_time DATETIME,
            PRIMARY KEY (logical_date, time_slot)
        )
    ''')
    _conn.execute('''
        CREATE TABLE IF NOT EXISTS picking_progress_snapshot_wave (
            logical_date TEXT,
            time_slot TEXT,
            wave_name TEXT,
            total_picked INTEGER DEFAULT 0,
            snapshot_time DATETIME,
            PRIMARY KEY (logical_date, time_slot, wave_name)
        )
    ''')
    _conn.execute('''
        CREATE TABLE IF NOT EXISTS sales_forecast (
            logical_date TEXT PRIMARY KEY,
            forecast_qty INTEGER DEFAULT 0,
            fetched_at DATETIME
        )
    ''')
    _conn.execute('''
        CREATE TABLE IF NOT EXISTS team_attendance_hourly (
            attendance_date TEXT,
            hour_slot INTEGER,
            team_name TEXT,
            head_count INTEGER DEFAULT 0,
            PRIMARY KEY (attendance_date, hour_slot, team_name)
        )
    ''')
    _conn.execute('''
        CREATE TABLE IF NOT EXISTS team_member_config (
            name TEXT PRIMARY KEY,
            team_name TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    _conn.execute('''
        CREATE TABLE IF NOT EXISTS attendance_gantt_detail (
            attendance_date TEXT,
            name TEXT,
            team_name TEXT,
            start_time TEXT,
            end_time TEXT,
            PRIMARY KEY (attendance_date, name)
        )
    ''')
    _conn.commit()
    _conn.close()
