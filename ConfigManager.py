from typing import Dict, List, Optional, Any


class ConfigManager:
    """爬虫配置管理类，使用硬编码的站点配置（模板示例）"""

    def __init__(self):
        """初始化配置管理器，使用硬编码配置"""
        # 新闻采集配置模板 - 请根据目标网站修改
        self.categories_config = {
            "栏目1_标准列表": {
                "base_url": "***",
                "base_domain": "***",
                "items_per_page": 15,
                "is_active": True,
                "total_count_xpath": ["//div[@class='pb_sys_common pb_sys_normal pb_sys_style1']/span[2]/text()"],
                "news_links_xpath": ["//ul[@class='list']//a/@href"],
                "news_titles_xpath": ["//ul[@class='list']//a/@title"],
                "title_xpath": ["//h1/text()", "//title/text()"],
                "date_xpath": ["//div/form/p//text()"],
                "author_xpath": ["//div/form/p//text()"],
                "content_xpath": ["//div[@class='v_news_content']//text()"],
                "image_xpath": ["//div[@class='v_news_content']//img/@src"],
                "video_xpath": ["//div[@class='v_news_content']//video/source/@src"],
                "valid_url_patterns": ["***", "https://mp.weixin.qq.com"],
                "special_handling": {
                    "metadata_xpath": "//div/form/p"
                }
            },
            "栏目2_标准列表": {
                "base_url": "***",
                "base_domain": "***",
                "items_per_page": 15,
                "is_active": True,
                "total_count_xpath": ["//div[@class='pb_sys_common pb_sys_normal pb_sys_style1']/span[2]/text()"],
                "news_links_xpath": ["//ul[@class='list']//a/@href"],
                "news_titles_xpath": ["//ul[@class='list']//a/@title"],
                "title_xpath": ["//h1/text()", "//title/text()"],
                "date_xpath": ["//div/form/p//text()"],
                "author_xpath": ["//div/form/p//text()"],
                "content_xpath": ["//div[@class='v_news_content']//text()"],
                "image_xpath": ["//div[@class='v_news_content']//img/@src"],
                "video_xpath": ["//div[@class='v_news_content']//video/source/@src"],
                "valid_url_patterns": ["***", "https://mp.weixin.qq.com"],
                "special_handling": {
                    "metadata_xpath": "//div/form/p"
                }
            },
            "栏目3_多URL源": {
                "base_url": [
                    "***",
                    "***",
                    "***"
                ],
                "base_domain": "***",
                "items_per_page": 10,
                "is_active": True,
                "total_count_xpath": ["//div[@class='pb_sys_common pb_sys_normal pb_sys_style1']/span/text()"],
                "news_links_xpath": ["//ul[@class='flex']//a/@href"],
                "news_titles_xpath": ["//ul[@class='flex']//a/@title"],
                "title_xpath": ["//h1/text()", "//title/text()"],
                "date_xpath": ["//div/form/p//text()"],
                "author_xpath": ["//div/form/p//text()"],
                "content_xpath": ["//div[@class='v_news_content']//text()"],
                "image_xpath": ["//div[@class='v_news_content']//img/@src"],
                "video_xpath": ["//div[@class='v_news_content']//video/source/@src"],
                "valid_url_patterns": ["***", "https://mp.weixin.qq.com"],
                "special_handling": {
                    "metadata_xpath": "//div/form/p",
                    "link_filter_pattern": "***",  # 只保留包含此模式的链接
                    "multiple_base_urls": True  # 标记为多URL栏目
                }
            },
            "栏目4_特殊页面结构": {
                "base_url": "***",
                "base_domain": "***",
                "items_per_page": 15,
                "is_active": False,  # 默认不启用，使用专用采集器
                "total_count_xpath": ["//div[@class='pb_sys_common pb_sys_normal pb_sys_style1']/span[2]/text()"],
                "news_links_xpath": ["//ul[@class='list']//a/@href"],
                "news_titles_xpath": ["//ul[@class='list']//a/@title"],
                "title_xpath": ["//h1/text()", "//title/text()"],
                "date_xpath": ["//div/form/p//text()"],
                "author_xpath": ["//div/form/p//text()"],
                "content_xpath": ["//div[@class='v_news_content']//text()"],
                "image_xpath": ["//div[@class='v_news_content']//img/@src"],
                "video_xpath": ["//div[@class='v_news_content']//video/source/@src"],
                "valid_url_patterns": ["***", "https://mp.weixin.qq.com"],
                "special_handling": {
                    "metadata_xpath": "//div/form/p",
                    "use_custom_collector": True,  # 使用专用采集器
                    "custom_collector": "CustomCollector",  # 专用采集器名称
                    "sub_sources": {
                        # 子源A：列表页
                        "sub_a": {
                            "urls": [
                                "***",
                                "***",
                                "***",
                                "***"
                            ],
                            "news_links_xpath": "//ul[@class='flex']//a/@href",
                            "news_titles_xpath": "//ul[@class='flex']//a/@title",
                            "link_filter_pattern": "***",
                            "items_per_page": 10
                        },
                        # 子源B：单页面（无分页）
                        "sub_b_single_page": {
                            "urls": ["***"],
                            "is_single_page": True,
                            "title_xpath": "***",
                            "content_xpath": "***",
                            "image_xpath": "***",
                            "no_metadata": True
                        },
                        # 子源C：单页面（无分页）
                        "sub_c_single_page": {
                            "urls": ["***"],
                            "is_single_page": True,
                            "title_xpath": "***",
                            "content_xpath": "***",
                            "image_xpath": "***",
                            "no_metadata": True
                        },
                        # 子源D：标准列表页
                        "sub_d_list": {
                            "urls": ["***"],
                            "news_links_xpath": "//ul[@class='list']//a/@href",
                            "news_titles_xpath": "//ul[@class='list']//a/@title",
                            "items_per_page": 15
                        }
                    }
                }
            }
        }

    def get_active_categories(self) -> Dict[str, any]:
        """
        获取所有启用的分类及其URL
        :return: 分类名称到URL的字典（URL可能是字符串或列表）
        """
        return {
            name: config["base_url"]
            for name, config in self.categories_config.items()
            if config.get("is_active", True)
        }

    def get_category_config(self, category_name: str) -> Optional[Dict[str, Any]]:
        """
        获取指定分类的完整配置
        :param category_name: 分类名称
        :return: 配置字典或None
        """
        return self.categories_config.get(category_name)

    def get_xpath_config(self, category_name: str, xpath_type: str) -> List[str]:
        """
        获取指定分类的特定xpath配置列表
        :param category_name: 分类名称
        :param xpath_type: xpath类型（如：news_links_xpath, news_titles_xpath等）
        :return: xpath列表
        """
        config = self.get_category_config(category_name)
        if config and config.get(xpath_type):
            xpath_list = config.get(xpath_type)
            if isinstance(xpath_list, list):
                return xpath_list
            elif isinstance(xpath_list, str):
                return [xpath_list]
        return []

    def get_content_xpaths(self, category_name: str) -> List[str]:
        """
        获取内容xpath列表（按优先级排序）
        :param category_name: 分类名称
        :return: xpath列表
        """
        config = self.get_category_config(category_name)
        if config and config.get('content_xpath'):
            return config['content_xpath']
        return []

    def get_valid_url_patterns(self, category_name: str) -> List[str]:
        """
        获取有效URL模式列表
        :param category_name: 分类名称
        :return: URL模式列表
        """
        config = self.get_category_config(category_name)
        if config and config.get('valid_url_patterns'):
            return config['valid_url_patterns']
        return []

    def get_base_domain(self, category_name: str) -> Optional[str]:
        """
        获取基础域名
        :param category_name: 分类名称
        :return: 基础域名或None
        """
        config = self.get_category_config(category_name)
        if config:
            return config.get('base_domain')
        return None

    def get_items_per_page(self, category_name: str) -> int:
        """
        获取每页条数
        :param category_name: 分类名称
        :return: 每页条数，默认15
        """
        config = self.get_category_config(category_name)
        if config:
            return config.get('items_per_page', 15)
        return 15

    def get_special_handling(self, category_name: str) -> Optional[Dict[str, Any]]:
        """
        获取特殊处理配置
        :param category_name: 分类名称
        :return: 特殊处理配置字典或None
        """
        config = self.get_category_config(category_name)
        if config and config.get('special_handling'):
            return config.get('special_handling')
        return {}

    def is_special_category(self, category_name: str) -> bool:
        """
        检查是否为特殊处理分类
        :param category_name: 分类名称
        :return: 是否特殊处理
        """
        config = self.get_category_config(category_name)
        if config and config.get('special_handling'):
            return config['special_handling'].get('special_xpath', False)
        return False

    def should_filter_non_future_net(self, category_name: str) -> bool:
        """
        检查是否需要过滤非目标域名的链接（默认不过滤）
        :param category_name: 分类名称
        :return: 是否过滤
        """
        return False

    def get_link_filter_pattern(self, category_name: str) -> Optional[str]:
        """
        获取链接过滤模式
        :param category_name: 分类名称
        :return: 过滤模式字符串或None
        """
        special_handling = self.get_special_handling(category_name)
        if special_handling:
            return special_handling.get('link_filter_pattern', None)
        return None

    def has_multiple_base_urls(self, category_name: str) -> bool:
        """
        检查分类是否有多个base_url
        :param category_name: 分类名称
        :return: 是否有多个base_url
        """
        special_handling = self.get_special_handling(category_name)
        if special_handling:
            return special_handling.get('multiple_base_urls', False)
        return False


# 全局配置管理器实例
config_manager = ConfigManager()
