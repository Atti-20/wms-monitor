# -*- coding: utf-8 -*-
"""
WMS 数据抓取模块
按功能拆分为：
- base: 公共请求工具
- picking: 拣货数据抓取与人效计算
- abnormal: 异常包裹巡检
- stagnant: 拣货卡单监控
- attendance: 考勤数据抓取（班组出勤统计）
"""
from spiders.base import get_shared_session, request_with_retry
from spiders.picking import process_and_save
from spiders.abnormal import fetch_abnormal_parcels
from spiders.stagnant import check_picking_stagnant
from spiders.attendance import refresh_team_attendance
from spiders.permission import check_and_fix_permissions
