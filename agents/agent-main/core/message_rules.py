"""
消息处理规则配置
"""
from typing import Dict, List


class MessageRules:
    """消息处理规则配置类"""

    # 强时效性关键词（必须注入时间 + 强提示）
    STRONG_TIME_KEYWORDS: List[str] = [
        "最新", "现在", "当前", "昨天","前天","今天","明天","后天", "大后天","今年", "前年","明年","后年","这个月", "最近", "刚刚","新款", "新品", "上市", "发布",
        "最火", "最热", "流行", "趋势",  "几点", "多久", "多长时间", "多少时间",
      "小时后", "分钟后", "天后", "小时前", "分钟前", "天前"
    ]

    # 中等时效性关键词（注入时间 + 轻提示）
    MEDIUM_TIME_KEYWORDS: List[str] = [
        "招聘", "工作", "职位", "岗位", "求职",
        "酒店", "宾馆", "住宿", "预订",
        "餐厅", "美食", "餐饮", "吃饭",
        "价格", "多少钱", "费用", "促销", "优惠",
        "手机", "电脑", "产品", "推荐"
    ]

    # 排除关键词（不注入时间）
    EXCLUDE_KEYWORDS: List[str] = [

    ]

    # 强提示模板
    # STRONG_TEMPLATE: str = """[当前日期是 {date}，用户询问的是"最新"信息，必须优先使用工具查询最新数据]
    #    用户输入：{user_input}"""
    STRONG_TEMPLATE: str = """[当前日期是 {date}]
       用户输入：{user_input}"""
    # 轻提示模板
    MEDIUM_TEMPLATE: str = """[当前日期是 {date}]
        用户输入：{user_input}"""

    @classmethod
    def get_strong_keywords(cls) -> List[str]:
        """获取强时效性关键词"""
        return cls.STRONG_TIME_KEYWORDS

    @classmethod
    def get_medium_keywords(cls) -> List[str]:
        """获取中等时效性关键词"""
        return cls.MEDIUM_TIME_KEYWORDS

    @classmethod
    def get_exclude_keywords(cls) -> List[str]:
        """获取排除关键词"""
        return cls.EXCLUDE_KEYWORDS

    @classmethod
    def get_strong_template(cls) -> str:
        """获取强提示模板"""
        return cls.STRONG_TEMPLATE

    @classmethod
    def get_medium_template(cls) -> str:
        """获取轻提示模板"""
        return cls.MEDIUM_TEMPLATE
