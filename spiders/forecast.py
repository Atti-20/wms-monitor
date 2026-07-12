# -*- coding: utf-8 -*-
"""
销售预测抓取模块
从魔数 BI 看板 (bi.sankuai.com/dashboard/205353) 抓取深圳凤岗仓的 TN 算法预测值。
每天 19:05 首次触发，之后每 30 分钟检测一次，直到数据库中有次日预测数据。

数据来源：快驴资源计划-算法TN预测看板(共享仓) → Tab "TN算法预测值(共享仓)"
目标仓库：深圳凤岗仓 (ID 428)
取值逻辑：通过 catdesk 浏览器打开看板，从第一个表格中匹配 共享仓名称=深圳凤岗仓 的预测件量
         存入数据库时 logical_date = 明天，这样明天查看时即可展示
"""
import subprocess
import json
import os
from datetime import datetime, timedelta
from database import get_db

DASHBOARD_URL = "https://bi.sankuai.com/dashboard/controller/205353"
TARGET_WAREHOUSE = "深圳凤岗仓"

# catdesk CLI 路径
CATDESK_PS1 = os.path.join(os.path.expanduser("~"), ".catdesk", "bin", "catdesk.ps1")

# 提取脚本：从第一个表格中找 共享仓名称 包含"凤岗"的行，提取预测件量
EXTRACT_SCRIPT = (
    '(function(){'
    'var tables=document.querySelectorAll("table");'
    'if(tables.length===0)return JSON.stringify({found:false,error:"no_tables"});'
    'var table=tables[0];'
    'var rows=table.querySelectorAll("tr");'
    'if(rows.length<2)return JSON.stringify({found:false,error:"no_rows"});'
    'var headers=[];'
    'var hc=rows[0].querySelectorAll("th,td");'
    'for(var h=0;h<hc.length;h++)headers.push(hc[h].innerText.trim());'
    'var nameIdx=-1,qtyIdx=-1;'
    'for(var h=0;h<headers.length;h++){'
    'if(headers[h]==="共享仓名称")nameIdx=h;'
    'if(headers[h].indexOf("预测件量")>=0)qtyIdx=h}'
    'if(nameIdx<0||qtyIdx<0)return JSON.stringify({found:false,error:"missing_columns",headers:headers});'
    'for(var r=1;r<rows.length;r++){'
    'var cells=rows[r].querySelectorAll("td");'
    'if(cells.length<=Math.max(nameIdx,qtyIdx))continue;'
    'var name=cells[nameIdx].innerText.trim();'
    'if(name.indexOf("凤岗")>=0){'
    'var qtyText=cells[qtyIdx].innerText.trim();'
    'var qty=parseInt(qtyText.replace(/,/g,""));'
    'return JSON.stringify({found:true,value:qty,raw:qtyText,warehouse:name})}}'
    'return JSON.stringify({found:false,error:"no_fenggang",rowCount:rows.length})})()'
)


def _run_catdesk_browser(action_json):
    """
    调用 catdesk browser-action，传入 JSON 字符串或 dict。
    通过临时文件传递 JSON 避免 PowerShell 转义问题。
    返回解析后的 dict 或 None。
    """
    import tempfile
    if isinstance(action_json, dict):
        action_str = json.dumps(action_json, ensure_ascii=False)
    else:
        action_str = action_json

    # 写入临时文件避免 shell 转义问题
    tmp_file = None
    try:
        tmp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8')
        tmp_file.write(action_str)
        tmp_file.close()

        cmd = ["pwsh", "-NoProfile", "-Command",
               f'& "{CATDESK_PS1}" browser-action (Get-Content -Raw "{tmp_file.name}")']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print(f"[预测] catdesk browser-action 失败: {result.stderr.strip()[:300]}")
            return None
        output = result.stdout.strip()
        if not output:
            return None
        return json.loads(output)
    except subprocess.TimeoutExpired:
        print("[预测] catdesk browser-action 超时")
        return None
    except json.JSONDecodeError as e:
        print(f"[预测] 解析 catdesk 返回值失败: {e}")
        return None
    except Exception as e:
        print(f"[预测] catdesk 执行异常: {e}")
        return None
    finally:
        if tmp_file and os.path.exists(tmp_file.name):
            os.unlink(tmp_file.name)


def fetch_forecast():
    """
    抓取深圳凤岗仓 T+1 的销售预测值。
    通过 catdesk 浏览器打开看板 → 等待表格加载 → 提取数据 → 写入数据库。
    返回预测件数(int)，失败返回 None。
    """
    tomorrow = datetime.now() + timedelta(days=1)
    logical_date = tomorrow.strftime("%Y-%m-%d")

    print(f"[预测] 开始抓取深圳凤岗仓 T+1({logical_date}) 预测值 (catdesk 浏览器)...")

    # 1. 导航到看板页面
    nav_result = _run_catdesk_browser({
        "action": "navigate",
        "url": DASHBOARD_URL,
        "waitUntil": "networkidle"
    })
    if not nav_result or not nav_result.get("success"):
        print(f"[预测] 导航失败: {nav_result}")
        return None

    title = nav_result.get("data", {}).get("title", "")
    print(f"[预测] 页面已加载, title={title}")

    # 检查是否被重定向到登录页
    if "登录" in title or "login" in title.lower():
        print("[预测] 页面跳转到登录页，catdesk 浏览器登录态可能过期")
        return None

    # 2. 等待表格数据渲染（BI 看板需要时间加载图表）
    _run_catdesk_browser({"action": "wait", "timeout": 8000})

    # 3. 检查表格是否加载完成，最多重试 2 次
    table_count = 0
    for attempt in range(3):
        chk = _run_catdesk_browser({
            "action": "evaluate",
            "script": "document.querySelectorAll('table').length"
        })
        if chk and chk.get("success"):
            table_count = chk.get("data", {}).get("result", 0)
            if isinstance(table_count, str):
                table_count = int(table_count)
        if table_count > 0:
            break
        print(f"[预测] 表格未加载完成(attempt {attempt + 1})，等待 5 秒...")
        _run_catdesk_browser({"action": "wait", "timeout": 5000})

    if table_count == 0:
        print("[预测] 页面无表格，可能加载失败")
        return None

    print(f"[预测] 检测到 {table_count} 个表格，开始提取数据...")

    # 4. 执行提取脚本
    eval_result = _run_catdesk_browser({
        "action": "evaluate",
        "script": EXTRACT_SCRIPT
    })
    if not eval_result or not eval_result.get("success"):
        print(f"[预测] 执行提取脚本失败: {eval_result}")
        return None

    # 解析提取结果
    result_str = eval_result.get("data", {}).get("result", "{}")
    if isinstance(result_str, str):
        try:
            result = json.loads(result_str)
        except json.JSONDecodeError:
            print(f"[预测] 解析提取结果失败: {result_str[:300]}")
            return None
    else:
        result = result_str

    if not result.get("found"):
        error = result.get("error", "unknown")
        print(f"[预测] 未找到深圳凤岗仓预测数据, 原因: {error}")
        return None

    forecast_qty = result["value"]
    print(f"[预测] 深圳凤岗仓 T+1({logical_date}) 预测件数: {forecast_qty} (原始值: {result.get('raw')})")

    # 5. 写入数据库
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO sales_forecast
                 (logical_date, forecast_qty, fetched_at)
                 VALUES (?, ?, ?)''',
              (logical_date, forecast_qty, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    print(f"[预测] 已写入数据库: {logical_date} = {forecast_qty}")
    return forecast_qty


def get_today_forecast():
    """获取今日预测值（从数据库读取，不触发抓取）"""
    logical_date = datetime.now().strftime("%Y-%m-%d")
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT forecast_qty FROM sales_forecast WHERE logical_date = ?', (logical_date,))
    row = c.fetchone()
    conn.close()
    return row["forecast_qty"] if row else None


def get_tomorrow_forecast():
    """获取次日预测值（从数据库读取，不触发抓取）"""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT forecast_qty FROM sales_forecast WHERE logical_date = ?', (tomorrow,))
    row = c.fetchone()
    conn.close()
    return row["forecast_qty"] if row else None


if __name__ == "__main__":
    result = fetch_forecast()
    if result:
        print(f"抓取成功: {result} 件")
    else:
        print("抓取失败")
