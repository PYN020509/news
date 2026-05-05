import re
import time
import os
import json
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import SqlClient  # 恢复数据库连接
from CollectDetail import Cl  # 重新启用详情采集模块
from ConfigManager import config_manager  # 导入配置管理器
from lxml import etree
import urllib3
import ssl

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Connection': 'close',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate'
}

class NewsCollector:
    def __init__(self):
        # 创建自定义的SSL适配器
        self.session = requests.Session()

        # 配置重试策略
        retry_strategy = Retry(
            total=5,  # 最大重试次数
            backoff_factor=2,  # 重试延迟系数
            status_forcelist=[429, 500, 502, 503, 504],  # 需要重试的HTTP状态码
            allowed_methods=["HEAD", "GET", "OPTIONS"]  # 允许重试的方法
        )

        # 创建适配器
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20
        )

        # 将适配器挂载到session
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # SSL配置
        self.session.verify = False  # 禁用 SSL 验证
        self.session.keep_alive = False

        # 从数据库获取配置，替代硬编码的base_urls
        self.base_urls = config_manager.get_active_categories()
        print(f"从数据库加载的活跃分类: {list(self.base_urls.keys())}")

        self.invalid_links_file = "invalid_links.txt"
        self.collected_news_file = "collected_news.txt"  # 用于保存采集到的新闻

    def collect_news(self):
        """采集各个新闻分类"""
        for category, base_url in self.base_urls.items():
            try:
                print(f"开始采集{category}...")

                # 检查是否有多个base_url
                if isinstance(base_url, list):
                    # 多个URL的情况（如成信人物）
                    print(f"{category} 有 {len(base_url)} 个采集源")
                    for idx, url in enumerate(base_url, 1):
                        print(f"采集第 {idx}/{len(base_url)} 个源: {url}")
                        self._collect_category(url, category)
                else:
                    # 单个URL的情况
                    self._collect_category(base_url, category)
            except Exception as e:
                print(f"采集{category}出错: {e}")

    def _safe_get(self, url, max_retries=3):
        """安全的GET请求，带重试机制"""
        for attempt in range(max_retries):
            try:
                print(f"正在请求: {url} (尝试 {attempt + 1}/{max_retries})")
                response = self.session.get(
                    url,
                    headers=headers,
                    verify=False,
                    timeout=(10, 30),  # (连接超时, 读取超时)
                    allow_redirects=True
                )
                response.raise_for_status()
                return response
            except requests.exceptions.SSLError as e:
                print(f"SSL错误 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    print(f"等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"多次重试后仍然失败，跳过此URL")
                    return None
            except requests.exceptions.ConnectionError as e:
                print(f"连接错误 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    print(f"等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                else:
                    return None
            except requests.exceptions.Timeout as e:
                print(f"请求超时 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                else:
                    return None
            except Exception as e:
                print(f"请求异常 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                else:
                    return None
        return None

    def _collect_category(self, base_url, category):
        """采集单个新闻分类页面"""
        try:
            response = self._safe_get(base_url)
            if not response:
                print(f"无法访问 {category} 页面: {base_url}")
                return

            response.encoding = response.apparent_encoding  # 自动检测编码
            html = etree.HTML(response.text)

            # 获取总页数 - 使用数据库配置
            total_pages = 1  # 默认总页数为1

            # 从数据库获取总条数xpath配置
            total_count_xpaths = config_manager.get_xpath_config(category, 'total_count_xpath')
            if total_count_xpaths:
                p_t_span = None
                for xpath in total_count_xpaths:
                    p_t_span = html.xpath(xpath)
                    if p_t_span:
                        print(f"使用xpath '{xpath}' 成功获取总条数")
                        break

                if p_t_span:
                    p_t_text = p_t_span[0]  # 获取第一个匹配的文本
                    print(f"获取到总条数信息: {p_t_text}")

                    # 提取数字
                    numbers = re.findall(r'\d+', p_t_text)
                    if numbers:
                        total_count = int(numbers[0])  # 获取第一个数字，如87
                        print(f"总条数: {total_count}")

                        # 从数据库获取每页条数配置
                        items_per_page = config_manager.get_items_per_page(category)
                        total_pages = (total_count + items_per_page - 1) // items_per_page  # 向上取整
                        print(f"{category} - 每页{items_per_page}条，计算总页数: {total_pages}")
                else:
                    print(f"所有总条数xpath都未获取到结果: {total_count_xpaths}")
            else:
                print(f"未找到分类 '{category}' 的总条数xpath配置，使用默认页数1")

            print(f"总页数: {total_pages}")

            # 遍历所有页面，从第一页到最后一页
            for page in range(1, total_pages + 1):
                page_url = self._get_page_url(base_url, page, total_pages)
                print(f"采集第 {page} 页: {page_url}")
                self._collect_page(page_url, category)
                time.sleep(1)  # 延时避免过快请求

        except Exception as e:
            print(f"获取分类页面错误: {e}")

    def _get_page_url(self, base_url, page, total_pages):
        """根据页面号构建页面 URL - 成信大学使用倒序分页"""
        if page == 1:
            return base_url  # 第1页使用基准URL
        else:
            # 成信大学的分页URL格式是倒序的：
            # 第1页 = cxyw.htm
            # 第2页 = cxyw/195.htm (total_pages - 1)
            # 第3页 = cxyw/194.htm (total_pages - 2)
            # ...
            # 最后一页 = cxyw/1.htm
            # 例如：总条数2926，每页15条，总页数196
            # 第2页应该是 cxyw/195.htm
            if base_url.endswith('.htm'):
                base_part = base_url[:-4]  # 去掉 .htm
                reverse_page = total_pages - page + 1  # 计算倒序页码
                return f"{base_part}/{reverse_page}.htm"
            else:
                # 如果不是.htm结尾，使用参数方式
                if '?' in base_url:
                    return f"{base_url}&page={page}"
                else:
                    return f"{base_url}?page={page}"

    def _collect_page(self, page_url, category):
        """采集单个页面的新闻"""
        try:
            page_response = self._safe_get(page_url)
            if not page_response:
                print(f"无法访问页面: {page_url}")
                return

            page_response.encoding = page_response.apparent_encoding  # 自动检测编码
            page_html = etree.HTML(page_response.text)

            # 获取新闻链接和标题 - 使用数据库配置
            # 从数据库获取xpath配置
            news_links_xpaths = config_manager.get_xpath_config(category, 'news_links_xpath')
            news_titles_xpaths = config_manager.get_xpath_config(category, 'news_titles_xpath')

            if not news_links_xpaths or not news_titles_xpaths:
                print(f"分类 '{category}' 缺少必要的xpath配置")
                return

            print(f"正在处理{category}页面: {page_url}")
            print(f"使用链接xpath: {news_links_xpaths}")
            print(f"使用标题xpath: {news_titles_xpaths}")

            # 尝试获取新闻链接
            news_links = []
            for xpath in news_links_xpaths:
                news_links = page_html.xpath(xpath)
                if news_links:
                    print(f"使用链接xpath '{xpath}' 成功获取到 {len(news_links)} 条链接")
                    break

            # 尝试获取新闻标题
            news_titles = []
            for xpath in news_titles_xpaths:
                news_titles = page_html.xpath(xpath)
                if news_titles:
                    print(f"使用标题xpath '{xpath}' 成功获取到 {len(news_titles)} 个标题")
                    break

            print(f"{category}页面 - 找到 {len(news_links)} 条新闻链接，{len(news_titles)} 个标题")

            # 检查是否为特殊处理分类（如学术报告）
            if config_manager.is_special_category(category):
                # 特殊分类处理逻辑 - 直接使用获取到的链接和标题
                for i in range(min(len(news_links), len(news_titles))):
                    link = news_links[i]
                    title = news_titles[i].strip() if news_titles[i] else '无标题'
                    if link:
                        self._process_news_item(link, title, category)
                    else:
                        print(f"未找到链接，标题: {title}")
            else:
                # 通用分类处理逻辑
                if news_links and news_titles:
                    # 如果成功获取到链接和标题，直接使用
                    for i in range(min(len(news_links), len(news_titles))):
                        link = news_links[i]
                        title = news_titles[i].strip() if news_titles[i] else '无标题'
                        if link:
                            self._process_news_item(link, title, category)
                        else:
                            print(f"未找到链接，标题: {title}")
                else:
                    # 后备方案：使用通用xpath
                    news_items = page_html.xpath("//ul[@class='ej_list']//a")
                    print(f"后备方案 - 找到 {len(news_items)} 条新闻")

                    for item in news_items:
                        link = item.get('href')
                        title = item.text

                        title = title.strip() if title else '无标题'
                        if link:
                            self._process_news_item(link, title, category)
                        else:
                            print(f"未找到链接，标题: {title}")

        except Exception as e:
            print(f"页面采集错误: {e}")
            print(f"页面URL: {page_url}")

    def _process_news_item(self, link, title, category):
        """处理单条新闻项"""
        try:
            # 处理相对路径
            original_link = link

            # 检查链接过滤模式（在解析相对路径之前）
            link_filter_pattern = config_manager.get_link_filter_pattern(category)
            if link_filter_pattern:
                # 如果配置了链接过滤模式，只保留包含该模式的链接
                if link_filter_pattern not in original_link:
                    print(f"  过滤链接（不匹配模式 '{link_filter_pattern}'）: {original_link}")
                    return

            link = self._resolve_link(link, category)
            print(f"处理新闻 [{category}]: {title}")
            print(f"  原始链接: {original_link}")
            print(f"  处理后链接: {link}")

            # 检查链接是否符合要求 - 使用数据库配置
            print(f"  检查链接有效性...")

            # 获取有效URL模式
            valid_patterns = config_manager.get_valid_url_patterns(category)

            # 检查特殊处理规则
            if config_manager.should_filter_non_future_net(category):
                # 媒体西油等需要过滤非未来网链接的分类
                if not any(link.startswith(pattern) for pattern in valid_patterns):
                    self._record_invalid_link(link)
                    print(f"  {category}栏目过滤链接: {link}, 已记录")
                    return
                else:
                    print(f"  {category}链接有效: {link}")
            else:
                # 其他栏目检查是否为有效域名
                if valid_patterns and not any(link.startswith(pattern) for pattern in valid_patterns):
                    self._record_invalid_link(link)
                    print(f"  无效链接: {link}, 已记录")
                    return
                else:
                    print(f"  {category}链接有效: {link}")

            # 检查数据库中是否已存在该URL（通过URL判断是否采集过）
            if not SqlClient.Cs.check_url_exists(link):
                print(f"采集新闻: {link}")
                # 启用详情采集并存储到数据库，传递列表页的标题
                Cl.Collect_dea(link, category, list_title=title)
            else:
                print(f"新闻已存在: {link}")

        except Exception as e:
            print(f"处理新闻项时出错: {e}")

    def _resolve_link(self, link, category=None):
        """处理链接为绝对路径 - 使用配置"""
        if not link.startswith('http'):
            # 从配置获取基础域名
            base_domain = config_manager.get_base_domain(category)
            if not base_domain:
                # 如果没有配置，使用默认域名
                base_domain = 'https://www.cuit.edu.cn'
                print(f"警告: 分类 '{category}' 没有配置基础域名，使用默认域名: {base_domain}")

            if link.startswith('/'):
                # 根据不同的基准 URL 处理相对路径
                link = base_domain + link
            elif link.startswith('../'):
                link = base_domain + '/' + link.lstrip('../')
            else:
                # 处理相对路径，如 info/1002/19989.htm
                link = base_domain + '/' + link
        return link

    def _record_invalid_link(self, link, title="", reason="有反爬机制"):
        """记录无效链接到文件和数据库"""
        try:
            # 记录到文件（保持原有功能）
            with open(self.invalid_links_file, "a", encoding="utf-8") as f:
                f.write(link + "\n")

            # 记录到数据库（ID由数据库自动生成）
            # 创建基本的字典信息
            invalid_dict = {
                "网站名称": "成都信息工程大学",
                "网站域名": link,
                "所属栏目": "无效链接",
                "标题": title if title else "无法获取标题",
                "发布时间": "",
                "作者": "",
                "具体新闻内容": [],
                "采集人": "档案馆",
                "无效原因": reason
            }

            # 插入数据库，标记为无效链接
            # 注意：is_valid_link有值时，record_id会自动设置为-1
            # 检查URL是否已存在，避免重复插入
            if not SqlClient.Cs.check_url_exists(link):
                sql = SqlClient.Cs.ins(
                    title=title if title else "无法获取标题",
                    url=link,
                    dict_=json.dumps(invalid_dict, ensure_ascii=False),
                    image_path_list=[],
                    video_path_list=[],
                    pdf_list=[],
                    info="{}",
                    is_valid_link=reason  # 标记为无效链接，record_id会自动设为-1
                )
                if sql:
                    print(f"无效链接已记录到数据库: {link}")
            else:
                print(f"无效链接已存在于数据库中，跳过插入: {link}")

        except Exception as e:
            print(f"记录无效链接时出错: {e}")



    def _save_news(self, title, link):
        """保存新闻标题和链接到文件"""
        try:
            with open(self.collected_news_file, "a", encoding="utf-8") as f:
                f.write(f"{title} - {link}\n")
        except Exception as e:
            print(f"保存新闻时出错: {e}")

if __name__ == "__main__":
    collector = NewsCollector()
    collector.collect_news()

