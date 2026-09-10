#!/usr/bin/env python3
"""Translate untranslated entries in zh_Hans django.po to Chinese."""
import re

TRANSLATIONS = {
    "before till": "截止前",
    "create time buckets for reporting.": "创建用于报告的时间段。",
    "Label shown in the scenario dropdown list": "在场景下拉列表中显示的标签",
    "Date of the last state change": "最后状态变更日期",
    "Restore from backup": "从备份恢复",
    "Release: You will lose ALL data in this scenario!": "发布：您将丢失此场景中的所有数据！",
    "Promote: All data will be copied to Production": "提升：所有数据将被复制到生产环境",
    "Run a sequence of tasks, and schedule it to run automatically.": "运行一系列任务，并安排其自动运行。",
    "Run a sequence of tasks.": "运行一系列任务。",
    "next scheduled run": "下次计划运行",
    "unique name": "唯一名称",
    "abort on failure": "失败时中止",
    "weekly schedule": "每周计划",
    "email on success": "成功时发送邮件",
    "List of emails, separated by commas": "邮件列表，以逗号分隔",
    "email on failure": "失败时发送邮件",
    "Failure launching task": "启动任务失败",
    "Couldn't delete file": "无法删除文件",
    "Error deleting file": "删除文件错误",
    "my reports": "我的报告",
    "Execute SQL": "执行 SQL",
    "measure management": "度量管理",
    "Copy source measure": "复制源度量",
    "to destination measure": "到目标度量",
    "Specifies whether this forecast record should be planned": "指定是否应对此预测记录进行计划",
    "multiplier": "乘数",
    "No editable fields found": "未找到可编辑字段",
    "total orders value 3 years ago": "3年前总订单金额",
    "orders adjustment value 3 years ago": "3年前订单调整金额",
    "total orders value 2 years ago": "2年前总订单金额",
    "orders adjustment value 2 years ago": "2年前订单调整金额",
    "total orders value 1 years ago": "1年前总订单金额",
    "orders adjustment value 1 years ago": "1年前订单调整金额",
    "order backlog": "订单积压",
    "forecast backlog": "预测积压",
    "order backlog value": "订单积压金额",
    "forecast backlog value": "预测积压金额",
    "backlog value": "积压金额",
    "total supply value": "总供应金额",
    "computed": "已计算",
    "Label to be displayed in the user interface": "在用户界面中显示的标签",
    "mode in future periods": "未来期间模式",
    "mode in past periods": "过去期间模式",
    "compute expression": "计算表达式",
    "Formula to compute values": "计算值的公式",
    "update expression": "更新表达式",
    "Formula executed when updating this field": "更新此字段时执行的公式",
    "controls whether or not this measure is visible by default": "控制此度量是否默认可见",
    "override measure": "覆盖度量",
    "measures": "度量",
    "forecast wizard": "预测向导",
    "standard deviation of forecast error": "预测误差标准差",
    "unit of measure": "度量单位",
    "inventory detail": "库存明细",
    "matrix detail": "矩阵明细",
    "calendars": "日历",
    "independent": "独立",
    "all together": "全部一起",
    "in ratio": "按比例",
    "Due date of the sales order": "销售订单截止日期",
    'Status of the demand. Only "open" and "quote" demands are planned': '需求状态。仅"开放"和"报价"状态的需求会被计划',
    "policy": "策略",
    "Defines how sales orders are shipped together": "定义销售订单如何一起发货",
    "MTO batch name": "按单生产批次名称",
    "make to stock": "按库存生产",
    "make to order": "按订单生产",
    "Weight of the item": "物料重量",
    "Volume of the item": "物料体积",
    "Period of cover in days": "覆盖天数",
    "Destination location to be replenished": "要补货的目标地点",
    "Source location shipping the item": "发货的源地点",
    "Transport lead time": "运输提前期",
    "A maximum shipping quantity": "最大发货数量",
    "batching window": "批次窗口",
    "Proposed distribution orders within this window will be grouped together": "此窗口内的建议配送订单将被合并在一起",
    "push priority": "推送优先级",
    "Priority for the push mode": "推送模式优先级",
    "Item produced by this operation": "此工序生产的物料",
    "Parent operation (which must be of type routing, alternate or split)": "父工序（必须为工艺路线、替代或拆分类型）",
    "Minimum production quantity": "最小生产数量",
    "Multiple production quantity": "生产批量倍数",
    "Maximum production quantity": "最大生产数量",
    "Cost per produced unit": "单位生产成本",
    "Fixed production time for setup and overhead": "设置和间接费用的固定生产时间",
    "Production time per produced piece": "每件产品的生产时间",
    "The solver algorithm will scan for opportunities to create batches within this time window before and after the requirement date": "求解器算法将在需求日期前后的此时间窗口内扫描创建批次的机会",
    "Batch transfer": "批量转移",
    "Quantity to consume or produce per piece": "每件消耗或生产的数量",
    "fixed quantity": "固定数量",
    "Fixed quantity to consume or produce": "消耗或生产的固定数量",
    "Name of this operation material to identify alternates": "此工序物料名称，用于识别替代项",
    "transfer batch quantity": "转移批量",
    "Batch size by in which material is produced or consumed": "物料生产或消耗的批量大小",
    "Time offset from the start or end to consume or produce material": "从开始或结束消耗或生产物料的时间偏移",
    "Required skill to perform the operation": "执行工序所需的技能",
    "Required quantity of the resource": "所需资源数量",
    "quantity fixed": "固定数量",
    "Constant part of the capacity consumption (bucketized resources only)": "产能消耗的固定部分（仅限分段资源）",
    "Name of this operation resource to identify alternates": "此工序资源名称，用于识别替代项",
    "Priority of this operation resource in a group of alternates": "此工序资源在替代组中的优先级",
    "blocked by operation": "被工序阻塞",
    "Quantity relation between the operations": "工序之间的数量关系",
    "soft safety lead time": "软安全提前期",
    "hard safety lead time": "硬安全提前期",
    "operation dependency": "工序依赖",
    "operation dependencies": "工序依赖",
    "delivery order": "配送订单",
    "scrap order": "报废订单",
    "completed quantity": "已完成数量",
    "load status": "负荷状态",
    "Status of the resource assignment": "资源分配状态",
    "resource detail": "资源明细",
    "material status": "物料状态",
    "status of the material production or consumption": "物料生产或消耗状态",
    "delivery orders": "配送订单",
    "constrained": "受限",
    "controls whether or not this resource is planned in finite capacity mode": "控制此资源是否以有限产能模式计划",
    "efficiency %": "效率%",
    "Efficiency percentage of the resource": "资源效率百分比",
    "efficiency % calendar": "效率%日历",
    "Calendar defining the efficiency percentage of the resource varying over time": "定义资源效率百分比随时间变化的日历",
    "Extra resource used during this changeover": "换线期间使用的额外资源",
    "A maximum purchasing quantity": "最大采购数量",
    "Proposed purchase orders within this window will be grouped together": "此窗口内的建议采购订单将被合并在一起",
    "dependencies": "依赖关系",
    "display kanban cards": "显示看板卡片",
    "display calendar": "显示日历",
    "net duration": "净时长",
    "quantity completed": "已完成数量",
    "setup end date": "设置结束日期",
    "setup duration": "设置时长",
    "setup duration override": "设置时长覆盖",
    "feasible": "可行",
    "in transit in %(loc)s at %(date)s": "在 %(date)s 于 %(loc)s 运输中",
    "received in %(loc)s between %(date1)s and %(date2)s": "在 %(date1)s 至 %(date2)s 期间于 %(loc)s 接收",
    "shipped from %(loc)s between %(date1)s and %(date2)s": "在 %(date1)s 至 %(date2)s 期间从 %(loc)s 发货",
    "total volume": "总体积",
    "total weight": "总重量",
    "end items": "最终产品",
    "Inventory detail": "库存明细",
    "operationplan quantity completed": "工序计划已完成数量",
    "work in progress in %(loc)s at %(date)s": "在 %(date)s 于 %(loc)s 在制品",
    "produced in %(loc)s between %(date1)s and %(date2)s": "在 %(date1)s 至 %(date2)s 期间于 %(loc)s 生产",
    "consumed in %(loc)s between %(date1)s and %(date2)s": "在 %(date1)s 至 %(date2)s 期间于 %(loc)s 消耗",
    "setups": "换线设置",
    "on order in %(loc)s at %(date)s": "在 %(date)s 于 %(loc)s 在订",
    "on order in %(loc)s between %(date1)s and %(date2)s": "在 %(date1)s 至 %(date2)s 期间于 %(loc)s 在订",
    "quantity planned short": "计划短缺数量",
    "delivered from %(loc)s between %(date1)s and %(date2)s": "在 %(date1)s 至 %(date2)s 期间从 %(loc)s 交付",
    "segment detail": "分段明细",
    "Successor part": "后继部件",
    "Compute safety stocks and reorder quantities for raw materials, intermediate items and end items.": "计算原材料、中间物料和最终产品的安全库存与再订货数量。",
    "Scan for excess inventory and propose return shipments to the origin location.": "扫描多余库存并建议退货到原始地点。",
    "Push mode": "推送模式",
    "Activate stock push mode which moves all inventory downstream": "激活库存推送模式，将所有库存向下游移动",
    "Overrides the decoupled lead time": "覆盖解耦提前期",
    "inventory policy": "库存策略",
    "inventory policies": "库存策略",
    "ROQ Minimum Period of Cover (days)": "再订货数量最小覆盖天数",
    "ROQ Maximum Period of Cover (days)": "再订货数量最大覆盖天数",
    "Safety Stock Minimum Period of Cover (days)": "安全库存最小覆盖天数",
    "Safety Stock Maximum Period of Cover (days)": "安全库存最大覆盖天数",
    "reorder point": "再订货点",
    "reorder point value": "再订货点值",
    "proposed purchase orders": "建议采购订单",
    "proposed distribution orders": "建议配送订单",
    "open purchase orders": "未结采购订单",
    "open purchase orders beyond lead time": "超出提前期的未结采购订单",
    "open distribution orders": "未结配送订单",
    "open distribution orders beyond lead time": "超出提前期的未结配送订单",
    "proposed purchase order value": "建议采购订单金额",
    "proposed distribution order value": "建议配送订单金额",
    "open purchase order value": "未结采购订单金额",
    "open purchase orders beyond lead time value": "超出提前期的未结采购订单金额",
    "open distribution order value": "未结配送订单金额",
    "open distribution orders beyond lead time value": "超出提前期的未结配送订单金额",
    "start inventory days of cover": "期初库存覆盖天数",
    "Last %(x)s months": "过去 %(x)s 个月",
    "Network status": "网络状态",
    "Stocking policy": "库存策略",
    "open distribution order beyond lead time": "超出提前期的未结配送订单",
    "count of late demands": "延迟需求计数",
    "quantity of late demands": "延迟需求数量",
    "value of late demand": "延迟需求金额",
    "count of unplanned demands": "未计划需求计数",
    "quantity of unplanned demands": "未计划需求数量",
    "value of unplanned demands": "未计划需求金额",
    "count of capacity overload problems": "产能超载问题计数",
    "analyze late demands": "分析延迟需求",
    "Spot the top items with many late demands": "找出延迟需求最多的物料",
    "value of late demands": "延迟需求金额",
    "number of late demands": "延迟需求数量",
    "produced by PO confirmed": "已确认采购订单生产",
    "produced by PO proposed": "建议采购订单生产",
    "produced by MO confirmed": "已确认制造订单生产",
    "produced by MO proposed": "建议制造订单生产",
    "produced by DO confirmed": "已确认配送订单生产",
    "produced by DO proposed": "建议配送订单生产",
    "consumed by MO confirmed": "已确认制造订单消耗",
    "consumed by MO proposed": "建议制造订单消耗",
    "consumed by SO": "销售订单消耗",
    "consumed by Fcst": "预测消耗",
    "consumed by DO confirmed": "已确认配送订单消耗",
    "consumed by DO proposed": "建议配送订单消耗",
    "total in progress": "在制总计",
    "work in progress MO": "制造订单在制品",
    "on order PO": "采购订单在订",
    "in transit DO": "配送订单在途",
    "total backlog": "总积压",
    "consumed confirmed": "已确认消耗",
    "produced confirmed": "已确认生产",
    "sales order backlog": "销售订单积压",
    "proposed shipping": "建议发货",
    "total shipping": "总发货",
    "proposed receiving": "建议接收",
    "total receiving": "总接收",
    "proposed in transit": "建议在途",
    "total in transit": "总在途",
    "proposed production": "建议生产",
    "total production": "总生产",
    "approved and confirmed production": "已批准和确认的生产",
    "proposed ordering": "建议订购",
    "total ordering": "总订购",
    "proposed on order": "建议在订",
    "total on order": "总在订",
    "total produced": "总生产量",
    "total consumed": "总消耗量",
    "total consumed confirmed": "总已确认消耗",
    "total consumed proposed": "总建议消耗",
    "consumed by MO": "制造订单消耗",
    "consumed by DO": "配送订单消耗",
    "total produced confirmed": "总已确认生产",
    "total produced proposed": "总建议生产",
    "produced by MO": "制造订单生产",
    "produced by DO": "配送订单生产",
    "produced by PO": "采购订单生产",
    "total in progress confirmed": "总已确认在制",
    "total in progress proposed": "总建议在制",
    "work in progress MO confirmed": "已确认制造订单在制品",
    "work in progress MO proposed": "建议制造订单在制品",
    "on order PO confirmed": "已确认采购订单在订",
    "on order PO proposed": "建议采购订单在订",
    "in transit DO confirmed": "已确认配送订单在途",
    "in transit DO proposed": "建议配送订单在途",
    "Manufacturing order summary": "制造订单汇总",
    "Purchase order summary": "采购订单汇总",
    "Distribution order summary": "配送订单汇总",
    "item description": "物料描述",
    "required quantity": "需求数量",
    "required quantity proposed": "建议需求数量",
    "required quantity confirmed": "已确认需求数量",
    "available calendar": "可用日历",
    "my report": "我的报告",
    "Saving the report failed": "保存报告失败",
    "format SQL query": "格式化 SQL 查询",
    "Add custom report": "添加自定义报告",
    "Runs all the partitions": "运行所有分区",
    "The application is currently not available.": "应用程序当前不可用。",
    "shelf life": "保质期",
    "expiring quantity": "即将过期数量",
    "required shelf life": "所需保质期",
    "reset shelf life": "重置保质期",
    "stock order": "库存订单",
    "stock orders": "库存订单",
    "scrap orders": "报废订单",
    "No materials": "无物料",
    "Update materials": "更新物料",
    "Select a manufacturing order": "选择制造订单",
    "Start the web service.": "启动 Web 服务。",
    "Stop the web service.": "停止 Web 服务。",
    "explore features": "探索功能",
    "item name": "物料名称",
    "Sales history in the last %(buckets)s": "过去 %(buckets)s 的销售历史",
    "a list of integers:eg 10  20  30": "整数列表，例如：10 20 30",
    "supplier name": "供应商名称",
    "lead time (in days)": "提前期（天）",
    "operation name": "工序名称",
    "fixed time": "固定时间",
    "time per piece": "单件时间",
    "resource name": "资源名称",
    "consumed items": "消耗物料",
    "component or subassembly item name": "部件或子装配物料名称",
    "consumed quantity": "消耗数量",
    "source location": "源地点",
    "source location name": "源地点名称",
    "transport time": "运输时间",
    "transport time (in days)": "运输时间（天）",
    "unique sales order name": "唯一销售订单名称",
    "Create sales order": "创建销售订单",
    "Data loading wizard for forecasting": "预测数据加载向导",
    "Data loading wizard for inventory planning": "库存计划数据加载向导",
    "Data loading wizard for production planning": "生产计划数据加载向导",
    "Import from odoo": "从 Odoo 导入",
    "Get started - Data loading wizard": "入门 - 数据加载向导",
    "Quickstart production planning": "快速开始生产计划",
    "Quickstart forecasting": "快速开始预测",
}

# Templates / long HTML strings with placeholders
LONG_TRANSLATIONS = [
    (
        'This page helps developers to learn and experiment with the\n'
        '<a class="text-decoration-underline" target="_blank" rel="noopener" href="https://en.wikipedia.org/wiki/Representational_state_transfer">REST</a>\n'
        'API of ccAPPS.<br>\n'
        'This API allows your application to exchange information with ccAPPS.<br>\n'
        '<br>\n'
        'The "list API" link takes you to the object list page.<br>\n'
        'And the "detail API" link takes you to a specific object when you enter the object\'s primary key.<br>\n'
        'In these pages you will be able to perform HTTP requests with methods GET, POST, OPTIONS, PUT, PATCH and DELETE.<br>\n'
        'The results can be shown in JSON format or as HTML in your browser.<br>\n'
        '<br>\n'
        "Using tools like 'wget' or 'curl' you can access the API from the command line.<br>\n"
        'For instance, to return the list of all sales orders in JSON format:<br><br>\n'
        '<div class="ms-4">\n'
        '<div class="mb-2">\n'
        'Using <a class="text-decoration-underline" href="https://en.wikipedia.org/wiki/Basic_access_authentication" target="_blank">basic authentication</a>:\n'
        '</div>\n'
        '<div class="card mb-3"><div class="card-body">\n'
        'wget --http-user=%(username)s --http-password=PASSWORD http://127.0.0.1:8000/api/input/demand/<br><br>\n'
        'curl -u %(username)s:PASSWORD http://127.0.0.1:8000/api/input/demand/\n'
        '</div></div>\n'
        '<div class="mb-2">\n'
        'Using <a class="text-decoration-underline" href="https://jwt.io/" target="_blank">JSON Web Token authentication</a> valid for the next <input id="jwt" class="d-inline form-control p-1" type="number" value="%(exp)s" min="0" step="1" style="width:4em; background:white"/>  days:\n'
        '</div>\n'
        '<div class="card mb-3"><div class="card-body">\n'
        'wget --header \'Authorization: Bearer %(token)s\' http://127.0.0.1:8000/api/input/demand/<br><br>\n'
        "curl -H 'Accept: application/json' --header 'Authorization: Bearer %(token)s' http://127.0.0.1:8000/api/input/demand/\n"
        '</div></div>\n'
        '</div>\n',
        '此页面帮助开发者学习和实验 ccAPPS 的\n'
        '<a class="text-decoration-underline" target="_blank" rel="noopener" href="https://en.wikipedia.org/wiki/Representational_state_transfer">REST</a>\n'
        'API。<br>\n'
        '此 API 允许您的应用程序与 ccAPPS 交换信息。<br>\n'
        '<br>\n'
        '"list API" 链接带您到对象列表页面。<br>\n'
        '"detail API" 链接在您输入对象主键后带您到特定对象。<br>\n'
        '在这些页面中，您可以使用 GET、POST、OPTIONS、PUT、PATCH 和 DELETE 方法执行 HTTP 请求。<br>\n'
        '结果可以以 JSON 格式或在浏览器中以 HTML 形式显示。<br>\n'
        '<br>\n'
        "使用 'wget' 或 'curl' 等工具，您可以从命令行访问 API。<br>\n"
        '例如，以 JSON 格式返回所有销售订单的列表：<br><br>\n'
        '<div class="ms-4">\n'
        '<div class="mb-2">\n'
        '使用 <a class="text-decoration-underline" href="https://en.wikipedia.org/wiki/Basic_access_authentication" target="_blank">基本认证</a>：\n'
        '</div>\n'
        '<div class="card mb-3"><div class="card-body">\n'
        'wget --http-user=%(username)s --http-password=PASSWORD http://127.0.0.1:8000/api/input/demand/<br><br>\n'
        'curl -u %(username)s:PASSWORD http://127.0.0.1:8000/api/input/demand/\n'
        '</div></div>\n'
        '<div class="mb-2">\n'
        '使用 <a class="text-decoration-underline" href="https://jwt.io/" target="_blank">JSON Web Token 认证</a>，有效期为未来 <input id="jwt" class="d-inline form-control p-1" type="number" value="%(exp)s" min="0" step="1" style="width:4em; background:white"/> 天：\n'
        '</div>\n'
        '<div class="card mb-3"><div class="card-body">\n'
        'wget --header \'Authorization: Bearer %(token)s\' http://127.0.0.1:8000/api/input/demand/<br><br>\n'
        "curl -H 'Accept: application/json' --header 'Authorization: Bearer %(token)s' http://127.0.0.1:8000/api/input/demand/\n"
        '</div></div>\n'
        '</div>'
    ),
    (
        '<span data-bs-toggle="tooltip" data-bs-placement="top" data-bs-html="true" data-bs-title="Respect the capacity limits of your resources.">Capacity: respect capacity limits</span>',
        '<span data-bs-toggle="tooltip" data-bs-placement="top" data-bs-html="true" data-bs-title="遵守资源的产能限制。">产能：遵守产能限制</span>'
    ),
    (
        '<span data-bs-toggle="tooltip" data-bs-placement="top" data-bs-html="true" data-bs-title="Don\'t generate any plans that start or end in the past.<br>Proposed and approved activities must all be in the future.<br>Only confirmed activities are allowed to be in the past.">Lead time: do not plan in the past</span>',
        '<span data-bs-toggle="tooltip" data-bs-placement="top" data-bs-html="true" data-bs-title="不生成任何开始或结束时间在过去计划。<br>建议和已批准的活动必须全部在未来。<br>只有已确认的活动允许在过去。">提前期：不在过去进行计划</span>'
    ),
    (
        '<span data-bs-toggle="tooltip" data-bs-placement="top" data-bs-html="true" data-bs-title="Don\'t propose any plans within a frozen time window (which is configured on each operation).<br>Within the frozen zone only approved and confirmed activities are allowed, no new proposed activities.">Release fence: do not plan within a frozen time window</span>',
        '<span data-bs-toggle="tooltip" data-bs-placement="top" data-bs-html="true" data-bs-title="不在冻结时间窗口内建议任何计划（在每个工序上配置）。<br>在冻结区域内，只允许已批准和已确认的活动，不允许新的建议活动。">发布边界：不在冻结时间窗口内计划</span>'
    ),
    (
        'You have a number of available scenario databases available.<br>\n        You can copy data into a scenario database to create a isolated sandbox for what-if analysis.<br>\n        Use the dropdown in the upper right corner of the screen to select which scenario you\'re working in.\n        ',
        '您有多个可用的场景数据库。<br>\n        您可以将数据复制到场景数据库中，以创建隔离的沙箱进行假设分析。<br>\n        使用屏幕右上角的下拉菜单选择您正在操作的场景。'
    ),
    (
        '<b>In use</b>: Contains data<br><b>Free</b>: Available to copy data into<br><b>Busy</b>: Data copy in progress',
        '<b>使用中</b>：包含数据<br><b>空闲</b>：可复制数据<br><b>忙碌</b>：数据复制进行中'
    ),
    (
        'Select all reports that will be pushed to the configured FTP/FTPS/SFTP folder.',
        '选择将推送到已配置的 FTP/FTPS/SFTP 文件夹的所有报告。'
    ),
    (
        'Analyze the sales history and compute a statistical forecast for the future',
        '分析销售历史并为未来计算统计预测'
    ),
    (
        '<span data-bs-toggle="tooltip" data-bs-placement="left" data-bs-html="true" data-bs-title="Sets safety stock to 0 and reorder quantity to 1.<br>This item-location will be planned with replenishments matching 1-to-1 with the demand." >Do not stock</span>',
        '<span data-bs-toggle="tooltip" data-bs-placement="left" data-bs-html="true" data-bs-title="将安全库存设为0，再订货数量设为1。<br>此物料-地点将按1对1匹配需求进行补货计划。">不备库存</span>'
    ),
]


def translate_po(input_file, output_file):
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Line by line approach
    lines = content.split('\n')
    output_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check if this starts a msgid block
        if line.startswith('msgid '):
            # Collect msgid lines
            msgid_content = line[7:].strip('"')
            msgid_lines = [line]
            i += 1
            while i < len(lines) and lines[i].startswith('"'):
                msgid_content += lines[i].strip('"')
                msgid_lines.append(lines[i])
                i += 1

            # Check for msgstr
            if i < len(lines) and lines[i].startswith('msgstr '):
                msgstr_line = lines[i]
                msgstr_content = msgstr_line[7:].strip('"')
                msgstr_lines = [msgstr_line]
                i += 1
                while i < len(lines) and lines[i].startswith('"'):
                    msgstr_content += lines[i].strip('"')
                    msgstr_lines.append(lines[i])
                    i += 1

                # If msgstr is empty and msgid is not empty
                if msgstr_content == '' and msgid_content.strip():
                    translation = None

                    # Normalize PO-escaped string: unescape \n and \", strip leading newline
                    def normalize(s):
                        return s.replace('\\n', '\n').replace('\\"', '"').lstrip('\n')

                    normalized = normalize(msgid_content)

                    # Check TRANSLATIONS dict (normalized comparison)
                    for key, val in TRANSLATIONS.items():
                        if normalize(key) == normalized:
                            translation = val
                            break

                    if translation is None:
                        # Check long translations
                        for eng, chn in LONG_TRANSLATIONS:
                            if normalize(eng) == normalized:
                                translation = chn
                                break

                    if translation:
                        # Output the msgid lines unchanged
                        for ml in msgid_lines:
                            output_lines.append(ml)
                        # Replace msgstr with translated version
                        # Handle multi-line translations
                        trans_lines = translation.split('\\n')
                        if len(trans_lines) > 1:
                            # But we need to handle actual newlines too
                            pass

                        # If translation contains newlines (from the \n in strings)
                        if '\n' in translation:
                            output_lines.append('msgstr ""')
                            for tline in translation.split('\n'):
                                # escape quotes properly
                                escaped_line = tline.replace('\\', '\\\\').replace('"', '\\"')
                                output_lines.append(f'"{escaped_line}\\n"')
                        else:
                            escaped_trans = translation.replace('"', '\\"')
                            output_lines.append(f'msgstr "{escaped_trans}"')
                    else:
                        # Keep original, still untranslated
                        for ml in msgid_lines:
                            output_lines.append(ml)
                        for sl in msgstr_lines:
                            output_lines.append(sl)
                else:
                    # Already has translation or empty msgid - keep as is
                    for ml in msgid_lines:
                        output_lines.append(ml)
                    for sl in msgstr_lines:
                        output_lines.append(sl)
            else:
                output_lines.extend(msgid_lines)
        else:
            output_lines.append(line)
            i += 1

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines))

    print(f"Written to {output_file}")

if __name__ == '__main__':
    import json
    input_po = '/home/c/ccAPPS-master/ccAPPSdb/locale/zh_Hans/LC_MESSAGES/django.po'
    output_po = '/home/c/ccAPPS-master/ccAPPSdb/locale/zh_Hans/LC_MESSAGES/django_updated.po'
    translate_po(input_po, output_po)
