import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional


def load_image_configuration(file_path=None, environment=None):
    """先读取镜像专用配置；进程环境优先，禁止插值和回显现场值。"""
    target = os.environ if environment is None else environment
    explicit = file_path is not None or bool(target.get("NEWS_CONFIG_FILE"))
    path = Path(file_path or target.get("NEWS_CONFIG_FILE") or "/app/config/news.env")
    if not path.exists():
        if explicit:
            raise RuntimeError("指定的镜像配置文件不存在")
        return False
    try:
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise RuntimeError("镜像配置必须为不超过一兆字节的普通文件")
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise RuntimeError("镜像配置文件不可读取或不是有效 UTF-8 文本") from None

    def value_text(name, value):
        if not re.fullmatch(r"(?:NEWS_|ERMS_)[A-Z0-9_]+|TZ", name):
            raise RuntimeError("镜像配置包含非采集服务参数")
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (int, float)):
            value = str(value)
        if not isinstance(value, str) or "\x00" in value:
            raise RuntimeError("镜像配置参数类型不正确：" + name)
        return value

    def unique_values(pairs):
        values = {}
        for name, value in pairs:
            if name in values:
                raise RuntimeError("镜像配置存在重复参数")
            values[name] = value
        return values

    def reject_constant(_value):
        raise ValueError("镜像 JSON 配置不允许非有限数值")

    values = {}
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(text, object_pairs_hook=unique_values, parse_constant=reject_constant)
        except (ValueError, TypeError):
            raise RuntimeError("镜像配置不是有效 JSON") from None
        if not isinstance(parsed, dict):
            raise RuntimeError("镜像 JSON 配置必须是参数对象")
        for name, value in parsed.items():
            values[name] = value_text(name, value)
    else:
        for line_number, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([A-Z][A-Z0-9_]*)\s*=(.*)", line)
            if not match:
                raise RuntimeError(f"镜像配置第 {line_number} 行格式不正确")
            name, value = match.groups()
            if name in values:
                raise RuntimeError(f"镜像配置第 {line_number} 行参数重复")
            value = value.strip()
            if value.startswith('"'):
                try:
                    value, ending = json.JSONDecoder().raw_decode(value)
                    trailing = match.group(2).strip()[ending:].strip()
                    if not isinstance(value, str) or (trailing and not trailing.startswith("#")):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise RuntimeError(f"镜像配置第 {line_number} 行双引号字符串不正确") from None
            elif value.startswith("'"):
                ending = value.find("'", 1)
                if ending < 0 or (value[ending + 1:].strip() and not value[ending + 1:].strip().startswith("#")):
                    raise RuntimeError(f"镜像配置第 {line_number} 行单引号字符串不正确")
                value = value[1:ending]
            else:
                value = re.sub(r"\s+#.*$", "", value).rstrip()
            values[name] = value_text(name, value)
    # 所有参数校验完成后一次写入，不能留下只加载了一半的现场配置。
    for name, value in values.items():
        target.setdefault(name, value)
    return True


class ConfigManager:
    """甘肃中医药大学三个归档栏目的配置及兼容访问器。"""

    def __init__(self):
        base_domain = "https://www.gszy.edu.cn"
        self.categories_config = {
            "学校要闻": {
                "base_url": f"{base_domain}/reception/news/list/3-10.htm",
                "list_api": f"{base_domain}/reception/news/list/ajaxNewsList",
                "detail_api": f"{base_domain}/reception/news/detail/ajaxDetail",
                "article_url_template": f"{base_domain}/reception/news/detail/{{id}}.htm",
                "type_param": "newsTypeId", "type_id": "10",
                "date_field": "release_time", "classification": "WY11", "category_code": "01",
            },
            "杏林动态": {
                "base_url": f"{base_domain}/reception/news/list/3-1.htm",
                "list_api": f"{base_domain}/reception/news/list/ajaxNewsList",
                "detail_api": f"{base_domain}/reception/news/detail/ajaxDetail",
                "article_url_template": f"{base_domain}/reception/news/detail/{{id}}.htm",
                "type_param": "newsTypeId", "type_id": "1",
                "date_field": "release_time", "classification": "WY12", "category_code": "02",
            },
            "通知公告": {
                "base_url": f"{base_domain}/reception/notice/list/index",
                "list_api": f"{base_domain}/reception/notice/list/ajaxNoticeList",
                "detail_api": f"{base_domain}/reception/notice/detail/ajaxDetail",
                "article_url_template": f"{base_domain}/reception/notice/detail/{{id}}.htm",
                "type_param": "noticeTypeId", "type_id": "81b537ee8f6c4dc3822efd0ff8c73baf",
                "date_field": "create_time", "classification": "WY13", "category_code": "03",
            },
        }
        for config in self.categories_config.values():
            config.update({
                "base_domain": base_domain, "items_per_page": 10, "is_active": True,
                "valid_url_patterns": [f"{base_domain}/reception/"], "special_handling": {},
            })

    def get_active_categories(self) -> Dict[str, Any]:
        """获取启用栏目的列表页面地址。"""
        return {name: config["base_url"] for name, config in self.categories_config.items()
                if config.get("is_active", True)}

    def get_category_config(self, category_name: str) -> Optional[Dict[str, Any]]:
        """获取指定栏目的完整配置。"""
        return self.categories_config.get(category_name)

    def get_xpath_config(self, category_name: str, xpath_type: str) -> List[str]:
        """保留原有解析模块使用的 XPath 配置访问方式。"""
        value = (self.get_category_config(category_name) or {}).get(xpath_type)
        if isinstance(value, str):
            return [value]
        return value if isinstance(value, list) else []

    def get_content_xpaths(self, category_name: str) -> List[str]:
        """获取正文 XPath 配置。"""
        return self.get_xpath_config(category_name, "content_xpath")

    def get_valid_url_patterns(self, category_name: str) -> List[str]:
        """获取允许的文章地址前缀。"""
        return (self.get_category_config(category_name) or {}).get("valid_url_patterns", [])

    def get_base_domain(self, category_name: str) -> Optional[str]:
        """获取栏目所属网站。"""
        return (self.get_category_config(category_name) or {}).get("base_domain")

    def get_items_per_page(self, category_name: str) -> int:
        """获取站点每页条数。"""
        return (self.get_category_config(category_name) or {}).get("items_per_page", 10)

    def get_special_handling(self, category_name: str) -> Dict[str, Any]:
        """保留原有特殊配置访问方式。"""
        return (self.get_category_config(category_name) or {}).get("special_handling", {})

    def is_special_category(self, category_name: str) -> bool:
        """检查是否启用特殊 XPath 解析。"""
        return self.get_special_handling(category_name).get("special_xpath", False)

    def should_filter_non_future_net(self, category_name: str) -> bool:
        """兼容旧调用方，地址验证由栏目地址前缀统一完成。"""
        return False

    def get_link_filter_pattern(self, category_name: str) -> Optional[str]:
        """获取兼容的链接过滤配置。"""
        return self.get_special_handling(category_name).get("link_filter_pattern")

    def has_multiple_base_urls(self, category_name: str) -> bool:
        """检查是否配置多个列表页面。"""
        return self.get_special_handling(category_name).get("multiple_base_urls", False)


config_manager = ConfigManager()
