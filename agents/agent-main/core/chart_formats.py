"""
图表格式库

存储所有支持的图表类型的详细格式说明和示例
"""

# 基础图表类型（最常用的 3 种，按需注入）
BASIC_CHART_TYPES = {
    "bar": {
        "name": "柱状图",
        "description": "适合对比数据",
        "keywords": ["柱状图", "柱状", "对比", "比较", "价格", "销量", "排名"],
    },
    "line": {
        "name": "折线图",
        "description": "适合趋势数据",
        "keywords": ["折线图", "折线", "趋势", "变化", "增长", "时间", "走势"],
    },
    "pie": {
        "name": "饼图",
        "description": "适合占比数据",
        "keywords": ["饼图", "占比", "份额", "比例", "分布"],
    }
}

# 高级图表格式库
CHART_FORMAT_LIBRARY = {
    "bar": {
        "name": "柱状图",
        "description": "适合对比数据",
        "keywords": ["柱状图", "柱状", "对比", "比较", "排名", "销量", "数量"],
        "format": """
**柱状图格式**：
<chart>
{
  "type": "bar",
  "title": "图表标题",
  "data": [数值1, 数值2, 数值3],
  "labels": ["标签1", "标签2", "标签3"],
  "unit": "单位"
}
</chart>

**示例**：
<chart>
{
  "type": "bar",
  "title": "月度销售额",
  "data": [120, 200, 150, 80, 110],
  "labels": ["1月", "2月", "3月", "4月", "5月"],
  "unit": "万元"
}
</chart>
"""
    },

    "bar_horizontal": {
        "name": "水平条形图",
        "description": "适合长标签的对比数据",
        "keywords": ["水平条形图", "水平柱状图", "横向柱状图", "条形图", "排行", "排名", "横向"],
        "format": """
**水平条形图格式**：
<chart>
{
  "type": "bar_horizontal",
  "title": "产品销量排行",
  "data": [450, 380, 220, 150, 100],
  "labels": ["产品A", "产品B", "产品C", "产品D", "产品E"],
  "unit": "件"
}
</chart>
"""
    },

    "bar_grouped": {
        "name": "分组柱状图",
        "description": "适合多组数据对比",
        "keywords": ["分组柱状图", "分组", "多组", "对比", "年度", "季度"],
        "format": """
**分组柱状图格式**：
<chart>
{
  "type": "bar_grouped",
  "title": "季度对比",
  "series": [
    {"name": "2023年", "data": [100, 120, 150, 180]},
    {"name": "2024年", "data": [120, 150, 180, 200]}
  ],
  "labels": ["Q1", "Q2", "Q3", "Q4"],
  "unit": "万元"
}
</chart>
"""
    },

    "bar_stacked": {
        "name": "堆叠柱状图",
        "description": "适合展示构成和总量",
        "keywords": ["堆叠柱状图", "堆叠", "构成", "组成", "占比"],
        "format": """
**堆叠柱状图格式**：
<chart>
{
  "type": "bar_stacked",
  "title": "收入构成",
  "series": [
    {"name": "产品收入", "data": [100, 120, 150]},
    {"name": "服务收入", "data": [50, 60, 70]},
    {"name": "其他收入", "data": [20, 25, 30]}
  ],
  "labels": ["1月", "2月", "3月"],
  "unit": "万元"
}
</chart>
"""
    },

    "line": {
        "name": "折线图",
        "description": "适合趋势数据",
        "keywords": ["折线图", "折线", "趋势", "变化", "增长", "时间", "走势"],
        "format": """
**折线图格式**：
<chart>
{
  "type": "line",
  "title": "用户增长趋势",
  "data": [100, 150, 200, 280, 350, 420, 500],
  "labels": ["1月", "2月", "3月", "4月", "5月", "6月", "7月"],
  "unit": "人"
}
</chart>
"""
    },

    "line_multi": {
        "name": "多线折线图",
        "description": "适合多条趋势对比",
        "keywords": ["多线折线图", "多线", "多条", "多条折线", "对比趋势", "多产品"],
        "format": """
**多线折线图格式**：
<chart>
{
  "type": "line_multi",
  "title": "产品对比",
      "data": [
    {"name": "产品A", "data": [100, 120, 150, 180, 200]},
    {"name": "产品B", "data": [80, 90, 110, 130, 150]},
    {"name": "产品C", "data": [60, 70, 85, 100, 120]}
  ],
  "labels": ["1月", "2月", "3月", "4月", "5月"],
  "unit": "万元"
}
</chart>
"""
    },

    "area": {
        "name": "面积图",
        "description": "适合展示累积趋势",
        "keywords": ["面积图", "面积", "累积", "流量"],
        "format": """
**面积图格式**：
<chart>
{
  "type": "area",
  "title": "流量趋势",
  "data": [1000, 1200, 1500, 1800, 2000, 2200],
  "labels": ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"],
  "unit": "次"
}
</chart>
"""
    },

    "area_stacked": {
        "name": "堆叠面积图",
        "description": "适合展示多维度累积趋势",
        "keywords": ["堆叠面积图", "堆叠面积", "来源", "渠道"],
        "format": """
**堆叠面积图格式**：
<chart>
{
  "type": "area_stacked",
  "title": "流量来源",
  "data": [
    {"name": "直接访问", "data": [300, 350, 400, 450, 500]},
    {"name": "搜索引擎", "data": [200, 250, 300, 350, 400]},
    {"name": "社交媒体", "data": [100, 120, 150, 180, 200]}
  ],
  "labels": ["1月", "2月", "3月", "4月", "5月"],
  "unit": "次"
}
</chart>
"""
    },

    "pie": {
        "name": "饼图",
        "description": "适合占比数据",
        "keywords": ["饼图", "占比", "份额", "比例", "分布"],
        "format": """
**饼图格式**：
<chart>
{
  "type": "pie",
  "title": "市场份额",
  "data": [35, 25, 20, 15, 5],
  "labels": ["产品A", "产品B", "产品C", "产品D", "其他"],
  "unit": "%"
}
</chart>
"""
    },

    "donut": {
        "name": "甜甜圈图",
        "description": "适合占比数据（带中心文字）",
        "keywords": ["甜甜圈图", "甜甜圈", "环形图", "环形", "占比"],
        "format": """
**甜甜圈图格式**：
<chart>
{
  "type": "donut",
  "title": "用户分布",
  "data": [40, 30, 20, 10],
  "labels": ["18-25岁", "26-35岁", "36-45岁", "46岁以上"],
  "unit": "%",
  "centerText": "总用户\\n10000"
}
</chart>
"""
    },

    "scatter": {
        "name": "散点图",
        "description": "适合分布和相关性分析",
        "keywords": ["散点图", "散点", "分布", "相关性", "关系"],
        "format": """
**散点图格式**：
<chart>
{
  "type": "scatter",
  "title": "身高体重分布",
  "data": [
    {"x": 160, "y": 50},
    {"x": 165, "y": 55},
    {"x": 170, "y": 60},
    {"x": 175, "y": 65},
    {"x": 180, "y": 70}
  ],
  "xLabel": "身高(cm)",
  "yLabel": "体重(kg)"
}
</chart>
"""
    },

    "bubble": {
        "name": "气泡图",
        "description": "适合三维数据展示",
        "keywords": ["气泡图", "气泡", "三维", "多维"],
        "format": """
**气泡图格式**：
<chart>
{
  "type": "bubble",
  "title": "产品分析",
  "data": [
    {"x": 100, "y": 80, "size": 50, "name": "产品A"},
    {"x": 120, "y": 90, "size": 80, "name": "产品B"},
    {"x": 90, "y": 70, "size": 30, "name": "产品C"}
  ],
  "xLabel": "销量",
  "yLabel": "满意度",
  "sizeLabel": "市场份额"
}
</chart>
"""
    },

    "heatmap": {
        "name": "热力图",
        "description": "适合时段、区域等二维数据",
        "keywords": ["热力图", "热力", "热图", "时段", "活跃", "密度"],
        "format": """
**热力图格式**：
<chart>
{
  "type": "heatmap",
  "title": "活跃时段分析",
  "data": [
    [5, 10, 15, 20, 25, 30, 35],
    [8, 12, 18, 24, 30, 36, 42],
    [10, 15, 22, 30, 38, 45, 52]
  ],
  "xLabels": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
  "yLabels": ["00-08", "08-16", "16-24"],
  "unit": "次"
}
</chart>
"""
    },

    "funnel": {
        "name": "漏斗图",
        "description": "适合转化流程分析",
        "keywords": ["漏斗图", "漏斗", "转化", "流程", "步骤"],
        "format": """
**漏斗图格式**：
<chart>
{
  "type": "funnel",
  "title": "销售漏斗",
  "data": [
    {"name": "访问", "value": 10000},
    {"name": "注册", "value": 5000},
    {"name": "咨询", "value": 2000},
    {"name": "下单", "value": 800},
    {"name": "支付", "value": 600}
  ],
  "unit": "人",
  "showConversion": true
}
</chart>
"""
    },

    "combo": {
        "name": "柱线组合图",
        "description": "适合双轴数据对比",
        "keywords": ["柱线组合图", "柱线组合", "组合图", "组合", "双轴", "柱线", "增长率"],
        "format": """
**柱线组合图格式**：
<chart>
{
  "type": "combo",
  "title": "销售额与增长率",
  "barData": [
    {"name": "销售额", "data": [100, 120, 150, 180, 200]}
  ],
  "lineData": [
    {"name": "增长率", "data": [0, 20, 25, 20, 11]}
  ],
  "labels": ["1月", "2月", "3月", "4月", "5月"],
  "leftUnit": "万元",
  "rightUnit": "%"
}
</chart>
"""
    },

    "radar": {
        "name": "雷达图",
        "description": "适合多维度能力评估和对比",
        "keywords": ["雷达图", "雷达", "能力", "评估", "技能", "维度", "对比", "多维度"],
        "format": """
**雷达图格式**：
<chart>
{
  "type": "radar",
  "title": "前端开发技能评估",
  "labels": ["HTML/CSS", "JavaScript", "React", "Vue", "TypeScript", "Node.js"],
  "data": [
    {
      "label": "张三",
      "data": [90, 85, 80, 60, 75, 70]
    },
    {
      "label": "李四",
      "data": [70, 75, 65, 90, 80, 85]
    }
  ],
  "unit": "分"
}
</chart>
"""
    },

    "card_stat": {
        "name": "统计卡片",
        "description": "适合关键指标展示",
        "keywords": ["统计卡片", "卡片", "统计", "指标", "概览"],
        "format": """
**统计卡片格式**：
<chart>
{
  "type": "card_stat",
  "title": "今日数据",
  "data": [
    {
      "label": "访问量",
      "value": "12,345",
      "change": "+15%",
      "trend": "up",
      "icon": "👁️"
    },
    {
      "label": "销售额",
      "value": "¥98,765",
      "change": "+8%",
      "trend": "up",
      "icon": "💰"
    }
  ]
}
</chart>
"""
    }
}


def get_chart_format(chart_type: str) -> str:
    """
    获取指定图表类型的格式说明

    Args:
        chart_type: 图表类型

    Returns:
        格式说明字符串
    """
    if chart_type in CHART_FORMAT_LIBRARY:
        return CHART_FORMAT_LIBRARY[chart_type]["format"]
    return ""


def detect_chart_intent(query: str) -> bool:
    """
    检测用户查询是否需要图表可视化

    Args:
        query: 用户查询文本

    Returns:
        是否需要图表
    """
    query_lower = str(query).lower()
    # 明确需要图表的关键词
    chart_intent_keywords = [
        # 数据相关
        "数据", "统计", "分析", "对比", "比较", "趋势", "占比", "份额",
        # 可视化相关
        "图表", "柱状图", "折线图", "饼图", "表格", "可视化", "展示", "绘制", "画", "生成", "创建", "制作",
        # 图表类型（完整名称）
        "多线折线图", "分组柱状图", "堆叠柱状图", "水平条形图", "条形图",
        "面积图", "堆叠面积图", "甜甜圈图", "环形图",
        "散点图", "气泡图", "热力图", "热图", "漏斗图",
        "柱线组合图", "组合图", "雷达图", "统计卡片",
        # 报告相关
        "报告", "报表", "总结", "汇总", "概览",
        # 查询相关（可能需要数据展示）
        "价格", "销量", "排名", "排行", "增长", "下降", "变化",
        # 商品对比
        "京东", "淘宝", "拼多多", "抖音", "哪个便宜", "多少钱"
    ]

    # 明确不需要图表的关键词（优先级更高）
    no_chart_keywords = [
        # "天气", "时间", "是什么", "怎么样", "为什么", "如何",
        # "帮我", "告诉我", "解释", "介绍", "定义"
    ]

    # 先检查不需要图表的情况
    if any(kw in query_lower for kw in no_chart_keywords):
        # 但如果同时包含数据关键词，还是需要图表
        if not any(kw in query_lower for kw in ["数据", "统计", "对比", "价格"]):
            return False

    # 检查是否需要图表
    return any(kw in query_lower for kw in chart_intent_keywords)


def detect_needed_charts_smart(query: str) -> dict:
    """
    智能检测需要的图表类型

    Args:
        query: 用户查询文本

    Returns:
        {
            "need_charts": bool,  # 是否需要图表
            "basic": list,        # 需要的基础图表
            "advanced": list      # 需要的高级图表
        }
    """
    result = {
        "need_charts": False,
        "basic": [],
        "advanced": []
    }

    # 第一步：检测是否需要图表
    if not detect_chart_intent(query):
        return result

    result["need_charts"] = True
    query_lower = query.lower()

    # 第二步：检测基础图表类型
    for chart_type, info in BASIC_CHART_TYPES.items():
        keywords = info.get("keywords", [])
        if any(kw in query_lower for kw in keywords):
            result["basic"].append(chart_type)

    # 第三步：检测高级图表类型
    for chart_type, info in CHART_FORMAT_LIBRARY.items():
        if chart_type in BASIC_CHART_TYPES:
            continue
        keywords = info.get("keywords", [])
        if any(kw in query_lower for kw in keywords):
            result["advanced"].append(chart_type)

    # 特殊场景：研究报告、数据分析等 → 注入所有图表（基础 + 高级）
    report_keywords = ["报告", "报表", "分析报告", "研究报告", "数据分析", "对比", "分析", "研究"]
    if any(kw in query_lower for kw in report_keywords):
        # 注入所有基础图表
        result["basic"] = ["bar", "line", "pie"]
        # 注入所有高级图表
        result["advanced"] = [
            chart_type for chart_type in CHART_FORMAT_LIBRARY.keys()
            if chart_type not in BASIC_CHART_TYPES
        ]

    # 如果没有匹配到任何图表，但需要图表 → 默认注入基础 4 种
    if result["need_charts"] and not result["basic"] and not result["advanced"]:
        result["basic"] = ["bar", "line", "pie"]

    return result


def detect_needed_charts(query: str) -> list:
    """
    根据用户查询检测需要的图表类型（旧版本，保留兼容性）

    Args:
        query: 用户查询文本

    Returns:
        需要的图表类型列表
    """
    needed = []
    query_lower = str(query).lower()

    for chart_type, info in CHART_FORMAT_LIBRARY.items():
        # 跳过基础图表（已在系统提示词中）
        if chart_type in ["bar", "line", "pie"]:
            continue

        # 检查关键词
        keywords = info.get("keywords", [])
        if any(kw in query_lower for kw in keywords):
            needed.append(chart_type)

    return needed


def build_basic_chart_prompt(chart_types: list) -> str:
    """
    构建基础图表格式提示词

    Args:
        chart_types: 图表类型列表

    Returns:
        格式提示词字符串
    """
    if not chart_types:
        return ""

    prompt = "\n## 📊 图表格式\n\n"
    prompt += "### 图表标签\n"
    prompt += "<chart>\n"
    prompt += "{\n"
    prompt += '  "type": "bar|line|pie",\n'
    prompt += '  "title": "图表标题",\n'
    prompt += '  "data": [数值1, 数值2, 数值3],\n'
    prompt += '  "labels": ["标签1", "标签2", "标签3"],\n'
    prompt += '  "unit": "单位"\n'
    prompt += "}\n"
    prompt += "</chart>\n\n"

    prompt += "**可用图表类型**：\n"
    for chart_type in chart_types:
        if chart_type in BASIC_CHART_TYPES:
            info = BASIC_CHART_TYPES[chart_type]
            prompt += f"- `{chart_type}`: {info['description']}\n"

    return prompt


def build_chart_format_prompt(chart_types: list) -> str:
    """
    构建图表格式提示词

    Args:
        chart_types: 图表类型列表

    Returns:
        格式提示词字符串
    """
    if not chart_types:
        return ""

    prompt = "\n## 📊 高级图表格式\n\n"
    prompt += "根据你的查询，以下是可用的高级图表格式：\n\n"

    for chart_type in chart_types:
        if chart_type in CHART_FORMAT_LIBRARY:
            info = CHART_FORMAT_LIBRARY[chart_type]
            prompt += f"### {info['name']} ({chart_type})\n"
            prompt += f"{info['description']}\n\n"
            prompt += info["format"]
            prompt += "\n"

    return prompt
