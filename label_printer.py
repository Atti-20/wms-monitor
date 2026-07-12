# -*- coding: utf-8 -*-
"""
标签打印模块 - 使用 Playwright 渲染 HTML 模板生成高质量标签图片
然后通过 win32print 发送到 Gprinter GP-1324D 热敏打印机。

渲染方式：HTML/CSS + JsBarcode + QRCode → Playwright 截图 → 打印
这样渲染质量与 klfe-print 官方完全一致（都是浏览器渲染引擎）。

两种标签：
  1. 首张标签（任务汇总签）：70x50mm
  2. 包裹标签：70x48mm

打印机：Gprinter GP-1324D, 203dpi
"""
import os
import io
import subprocess
import tempfile
import asyncio
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    Image = None

# ============ 配置 ============
DPI = 203
PRINTER_NAME = "Gprinter GP-1324D"

# 标签尺寸（像素 @ 203dpi）
FIRST_LABEL_W = int(70 * DPI / 25.4)   # ~559px
FIRST_LABEL_H = int(50 * DPI / 25.4)   # ~399px
PARCEL_LABEL_W = int(70 * DPI / 25.4)  # ~559px
PARCEL_LABEL_H = int(48 * DPI / 25.4)  # ~383px

# 模板路径
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
FIRST_LABEL_TEMPLATE = os.path.join(TEMPLATE_DIR, "label_first.html")
PARCEL_LABEL_TEMPLATE = os.path.join(TEMPLATE_DIR, "label_parcel.html")


def _read_template(path):
    """读取 HTML 模板文件"""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _fill_template(html, data):
    """简单模板变量替换 {{key}} → value"""
    for key, value in data.items():
        placeholder = '{{' + key + '}}'
        html = html.replace(placeholder, str(value))
    return html


def _build_first_label_html(task_data):
    """构建首张标签的 HTML"""
    html = _read_template(FIRST_LABEL_TEMPLATE)

    stations = task_data.get("stationAreaNos", [])
    station_text = "\u3001".join(stations) if isinstance(stations, list) else str(stations)

    data = {
        "allotInWarehouseName": task_data.get("allotInWarehouseName", ""),
        "fulfillmentWaveName": task_data.get("fulfillmentWaveName", ""),
        "logicAreaName": task_data.get("logicAreaName", ""),
        "skuQty": task_data.get("skuQty", 0),
        "parcelQty": task_data.get("parcelQty", 0),
        "stationAreaNos": station_text,
        "zonePickBillNo": task_data.get("zonePickBillNo", ""),
        "printerName": task_data.get("printerName", ""),
    }
    return _fill_template(html, data)


def _build_parcel_label_html(tag_data, task_data, index, total):
    """构建包裹标签的 HTML"""
    html = _read_template(PARCEL_LABEL_TEMPLATE)

    sku_name = tag_data.get("skuName", "")
    sku_unit = tag_data.get("skuUnitDesc", "")
    sku_full = f"{sku_name}({sku_unit})" if sku_unit else sku_name

    order_no = tag_data.get("orderNo", "")
    customer = tag_data.get("customerName", "")
    # 截短显示
    if len(order_no) > 16:
        order_no = order_no[:16] + "*"
    if len(customer) > 4:
        customer = customer[:3] + "*"

    poi_serial = tag_data.get("poiSerialCode", "")
    if poi_serial and poi_serial.isdigit():
        poi_serial = poi_serial.zfill(3)

    data = {
        "areaNo": tag_data.get("areaNo", ""),
        "stationAreaNo": tag_data.get("stationAreaNo", ""),
        "stationRouteSeq": tag_data.get("stationRouteSeq", ""),
        "warehouseShortName": tag_data.get("warehouseShortName", ""),
        "orderNo": order_no,
        "customerName": customer,
        "skuFullName": sku_full,
        "packageNo": tag_data.get("packageNo", ""),
        "transportCategoryAbbr": tag_data.get("transportCategoryAbbr", ""),
        "poiSerialCode": poi_serial,
        "index": index,
        "total": total,
        "phoneString": tag_data.get("phoneString", ""),
        "qrCodeContent": tag_data.get("qrCodeContent", tag_data.get("packageNo", "")),
        "fulfillmentWaveShortName": tag_data.get("fulfillmentWaveShortName", ""),
    }
    return _fill_template(html, data)


# ================================================================
#  Playwright 渲染引擎
# ================================================================
async def _render_html_to_image_async(html_content, width_px, height_px):
    """
    使用 Playwright 将 HTML 渲染为图片（异步版本）
    返回 PIL Image 对象（1-bit 黑白，适合热敏打印）
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            viewport={'width': width_px, 'height': height_px},
            device_scale_factor=1,
        )

        await page.set_content(html_content, wait_until='networkidle')
        # 等待条码/二维码渲染完成
        await page.wait_for_timeout(500)

        screenshot = await page.screenshot(
            type='png',
            clip={'x': 0, 'y': 0, 'width': width_px, 'height': height_px},
        )

        await browser.close()

    # 转为 PIL Image
    img = Image.open(io.BytesIO(screenshot))
    # 转为黑白（热敏打印机）
    img = img.convert('L').point(lambda x: 0 if x < 180 else 255, '1')
    return img


def _render_html_to_image(html_content, width_px, height_px):
    """
    同步包装器 - 使用 Playwright 渲染 HTML 为图片
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 如果在 Flask 等异步环境中，创建新线程运行
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run,
                    _render_html_to_image_async(html_content, width_px, height_px)
                )
                return future.result(timeout=30)
        else:
            return loop.run_until_complete(
                _render_html_to_image_async(html_content, width_px, height_px)
            )
    except RuntimeError:
        return asyncio.run(
            _render_html_to_image_async(html_content, width_px, height_px)
        )


async def _render_batch_async(html_list, sizes):
    """
    批量渲染多个 HTML 为图片（共享一个浏览器实例，更高效）
    html_list: [(html_content, width_px, height_px), ...]
    返回 PIL Image 列表
    """
    from playwright.async_api import async_playwright

    images = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for html_content, width_px, height_px in html_list:
            page = await browser.new_page(
                viewport={'width': width_px, 'height': height_px},
                device_scale_factor=1,
            )
            await page.set_content(html_content, wait_until='networkidle')
            await page.wait_for_timeout(300)

            screenshot = await page.screenshot(
                type='png',
                clip={'x': 0, 'y': 0, 'width': width_px, 'height': height_px},
            )
            await page.close()

            img = Image.open(io.BytesIO(screenshot))
            img = img.convert('L').point(lambda x: 0 if x < 180 else 255, '1')
            images.append(img)

        await browser.close()

    return images


def _render_batch(html_list):
    """同步批量渲染"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _render_batch_async(html_list, None))
                return future.result(timeout=120)
        else:
            return loop.run_until_complete(_render_batch_async(html_list, None))
    except RuntimeError:
        return asyncio.run(_render_batch_async(html_list, None))


# ================================================================
#  公开接口
# ================================================================
def render_first_label(task_data):
    """渲染首张标签，返回 PIL Image"""
    html = _build_first_label_html(task_data)
    return _render_html_to_image(html, FIRST_LABEL_W, FIRST_LABEL_H)


def render_parcel_label(tag_data, task_data, index, total):
    """渲染包裹标签，返回 PIL Image"""
    html = _build_parcel_label_html(tag_data, task_data, index, total)
    return _render_html_to_image(html, PARCEL_LABEL_W, PARCEL_LABEL_H)


def render_all_labels(print_data):
    """
    批量渲染所有标签（高效：共享浏览器实例）
    print_data: containerParcelPrintQuery 返回的数据列表
    返回 PIL Image 列表
    """
    html_list = []

    for task in print_data:
        # 首张标签
        html = _build_first_label_html(task)
        html_list.append((html, FIRST_LABEL_W, FIRST_LABEL_H))

        # 包裹标签
        tags = task.get("packageTags", [])
        total = len(tags)
        for idx, tag in enumerate(tags, 1):
            html = _build_parcel_label_html(tag, task, idx, total)
            html_list.append((html, PARCEL_LABEL_W, PARCEL_LABEL_H))

    if not html_list:
        return []

    return _render_batch(html_list)


# ================================================================
#  打印功能
# ================================================================
def print_labels_win32(images, printer_name=None):
    """
    使用 win32print + win32ui 直接打印
    """
    try:
        import win32print
        import win32ui
        from PIL import ImageWin
    except ImportError:
        return _print_labels_mspaint(images, printer_name)

    if not printer_name:
        printer_name = PRINTER_NAME

    printed = 0
    errors = []

    try:
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(printer_name)

        for i, img in enumerate(images):
            try:
                hdc.StartDoc(f"Label_{i}")
                hdc.StartPage()

                printer_dpi_x = hdc.GetDeviceCaps(88)  # LOGPIXELSX
                printer_dpi_y = hdc.GetDeviceCaps(90)  # LOGPIXELSY

                img_w, img_h = img.size
                scale_x = printer_dpi_x / DPI
                scale_y = printer_dpi_y / DPI
                print_w = int(img_w * scale_x)
                print_h = int(img_h * scale_y)

                img_rgb = img.convert("RGB")
                dib = ImageWin.Dib(img_rgb)
                dib.draw(hdc.GetHandleOutput(), (0, 0, print_w, print_h))

                hdc.EndPage()
                hdc.EndDoc()
                printed += 1

            except Exception as e:
                errors.append(f"Label {i}: {str(e)}")
                try:
                    hdc.AbortDoc()
                except:
                    pass

        hdc.DeleteDC()

    except Exception as e:
        errors.append(f"Printer connection failed: {str(e)}")

    return {"printed": printed, "total": len(images), "errors": errors}


def _print_labels_mspaint(images, printer_name=None):
    """备用方案：通过 mspaint 打印"""
    if not printer_name:
        printer_name = PRINTER_NAME

    printed = 0
    errors = []

    for i, img in enumerate(images):
        try:
            tmp_path = os.path.join(tempfile.gettempdir(), f"label_{i:04d}.bmp")
            img.save(tmp_path, "BMP")

            result = subprocess.run(
                ["mspaint", "/pt", tmp_path, printer_name],
                capture_output=True, timeout=10
            )

            if result.returncode == 0:
                printed += 1
            else:
                errors.append(f"Label {i}: returncode={result.returncode}")

            try:
                os.remove(tmp_path)
            except:
                pass

        except subprocess.TimeoutExpired:
            errors.append(f"Label {i}: timeout")
        except Exception as e:
            errors.append(f"Label {i}: {str(e)}")

    return {"printed": printed, "total": len(images), "errors": errors}


# ================================================================
#  工具函数
# ================================================================
def get_available_printers():
    """获取系统中可用的打印机列表"""
    try:
        import win32print
        printers = win32print.EnumPrinters(
            win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        )
        return [p[2] for p in printers]
    except ImportError:
        try:
            result = subprocess.run(
                ["wmic", "printer", "get", "name"],
                capture_output=True, text=True, timeout=5
            )
            lines = [l.strip() for l in result.stdout.strip().split('\n')
                     if l.strip() and l.strip() != 'Name']
            return lines
        except:
            return []


def check_printer_status(printer_name=None):
    """检查打印机状态"""
    if not printer_name:
        printer_name = PRINTER_NAME

    try:
        import win32print
        hprinter = win32print.OpenPrinter(printer_name)
        info = win32print.GetPrinter(hprinter, 2)
        win32print.ClosePrinter(hprinter)
        status = info.get('Status', 0)
        return {
            "name": printer_name,
            "online": status == 0,
            "status_code": status,
            "status_text": "就绪" if status == 0 else f"状态码: {status}",
        }
    except ImportError:
        printers = get_available_printers()
        found = printer_name in printers
        return {
            "name": printer_name,
            "online": found,
            "status_code": -1,
            "status_text": "已找到" if found else "未找到打印机",
        }
    except Exception as e:
        return {
            "name": printer_name,
            "online": False,
            "status_code": -1,
            "status_text": str(e),
        }
