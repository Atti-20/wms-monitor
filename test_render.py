# -*- coding: utf-8 -*-
"""测试 Playwright 渲染标签效果"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import label_printer

# 模拟首张标签数据
task_data = {
    'allotInWarehouseName': '东莞虎门仓',
    'fulfillmentWaveName': '上午达',
    'logicAreaName': '调料',
    'skuQty': 45,
    'parcelQty': 45,
    'stationAreaNos': ['A', 'B', 'C', 'K', 'L', 'X', 'Y', 'Z'],
    'zonePickBillNo': 'DBJ4282026053000011-1',
    'printerName': '程旭同4730425',
}

# 模拟包裹标签数据
tag_data = {
    'areaNo': '乙',
    'stationAreaNo': 'L',
    'stationRouteSeq': '06',
    'warehouseShortName': '平湖仓',
    'warehouseName': '东莞虎门仓',
    'orderNo': 'KL260530*11866',
    'customerName': '大食代',
    'skuName': '[农威]象牙粘米25kg（中国香米）',
    'skuUnitDesc': '25kg/袋',
    'packageNo': '250054626000000271055404',
    'transportCategoryAbbr': '大件区',
    'poiSerialCode': '001',
    'phoneString': '客服电话:400-0616-700',
    'qrCodeContent': '250054626000000271055404',
    'fulfillmentWaveShortName': '上午',
}

print("正在使用 Playwright 渲染标签...")
print("(首次运行可能需要几秒钟启动浏览器)")

# 渲染首张标签
first_img = label_printer.render_first_label(task_data)
first_img.save('preview_first_label.png')
print(f'首张标签: {first_img.size} -> preview_first_label.png')

# 渲染包裹标签
parcel_img = label_printer.render_parcel_label(tag_data, task_data, 1, 45)
parcel_img.save('preview_parcel_label.png')
print(f'包裹标签: {parcel_img.size} -> preview_parcel_label.png')

print('\n渲染完成！预览图已保存。')
