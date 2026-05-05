import re
import json
import hashlib
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from lxml import etree
from urllib.parse import urljoin
# # import ToPDF  # 不再使用原先的ToPDF
import urllib3
import logging
import SqlClient  # 恢复数据库连接
from ConfigManager import config_manager  # 导入配置管理器
import asyncio
from requests.exceptions import RequestException
import time

# 导入改进的PDF生成器
try:
    from ImprovedPDFGenerator import ImprovedPDFGenerator
    PDF_GENERATOR_AVAILABLE = True
except ImportError:
    PDF_GENERATOR_AVAILABLE = False
    print("ImprovedPDFGenerator不可用，将使用原有的PDF生成方法")

# 导入微信公众号内容提取器
try:
    from WeChatContentExtractor import WeChatContentExtractor
    WECHAT_EXTRACTOR_AVAILABLE = True
except ImportError:
    WECHAT_EXTRACTOR_AVAILABLE = False
    print("WeChatContentExtractor不可用，将使用原有的内容提取方法")

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 设置日志
logging.basicConfig(
    filename='collect_detail.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# 设置请求头
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Connection': 'close',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'
}

# 配置会话 - 添加重试策略
session = requests.Session()

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
session.mount("http://", adapter)
session.mount("https://", adapter)

# SSL配置
session.keep_alive = False
session.verify = False  # 禁用 SSL 验证


def safe_get(url, max_retries=3, **kwargs):
    """安全的GET请求，带重试机制"""
    for attempt in range(max_retries):
        try:
            response = session.get(
                url,
                headers=headers,
                verify=False,
                timeout=kwargs.get('timeout', 30),
                **{k: v for k, v in kwargs.items() if k != 'timeout'}
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
                raise
        except requests.exceptions.ConnectionError as e:
            print(f"连接错误 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2
                time.sleep(wait_time)
            else:
                raise
        except requests.exceptions.Timeout as e:
            print(f"请求超时 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise
        except Exception as e:
            print(f"请求异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                raise
    raise Exception(f"请求失败: {url}")


class Cl:
    @staticmethod
    def _handle_wechat_content(url, category):
        """
        处理微信公众号内容
        :param url: 微信公众号文章URL
        :param category: 分类
        """
        try:
            # 创建微信公众号提取器
            extractor = WeChatContentExtractor()

            # 提取内容
            extracted_data = extractor.extract_content(url)

            if 'error' in extracted_data:
                print(f"微信公众号内容提取失败: {extracted_data['error']}")
                logging.error(f"微信公众号内容提取失败: {extracted_data['error']}")
                return None

            # ID由数据库自动生成，不需要手动设置

            title = extracted_data['title']
            date = extracted_data['date']
            author = extracted_data['author']
            text = '\n'.join(extracted_data['content'])

            print(f"微信公众号内容提取成功:")
            print(f"  标题: {title}")
            print(f"  作者: {author}")
            print(f"  发布时间: {date}")
            print(f"  内容长度: {len(text)} 字符")
            print(f"  图片数量: {len(extracted_data['images'])}")

            # 创建保存目录 - 使用与普通新闻相同的路径结构
            category_path = os.path.join("/archive-file/upload/cuit", category)
            os.makedirs(category_path, exist_ok=True)

            # 保存图片（使用与普通新闻相同的哈希命名方式）
            image_path_list = []
            for i, img_url in enumerate(extracted_data['images']):
                try:
                    img_response = safe_get(img_url, timeout=30)
                    if img_response.status_code == 200:
                        # 使用哈希命名，与普通新闻保持一致
                        img_hash = hashlib.md5(f"{title}_{i+1}".encode('utf-8')).hexdigest()
                        img_filename = f"{img_hash}.jpg"
                        img_path = os.path.join(category_path, img_filename)
                        with open(img_path, 'wb') as f:
                            f.write(img_response.content)
                        image_path_list.append({f"{title}_{i+1}": img_path})
                        print(f"微信图片保存成功: {img_path}")
                except Exception as e:
                    print(f"微信图片保存失败: {e}")
                    logging.error(f"微信图片保存失败: {e}")

            # 视频处理（微信公众号通常没有视频，但保持接口一致）
            video_path_list = []

            # 生成PDF
            pdf_path = Cl._generate_pdf(url, category_path, title)

            # 构建数据字典
            dict_ = extractor.format_for_database(extracted_data, category)

            # 存储到数据库
            pdf_list = [{f"{title}.pdf": pdf_path}] if pdf_path else []

            sql = SqlClient.Cs.ins(
                title=title,
                url=url,
                dict_=json.dumps(dict_, ensure_ascii=False),
                image_path_list=image_path_list,
                video_path_list=video_path_list,
                pdf_list=pdf_list,
                info="{}",
                is_valid_link=None  # 有效链接
            )

            if sql:
                logging.info(f"微信公众号内容存入数据库成功：{sql}")
                print(f"微信公众号内容存入数据库成功")

            print(f"微信公众号处理完成: {title}")
            return True

        except Exception as e:
            print(f"微信公众号内容处理失败: {e}")
            logging.error(f"微信公众号内容处理失败: {e}")
            return None

    @staticmethod
    def Collect_dea(url, category, list_title=None):
        """
        收集单条新闻详情，并保存图片、生成PDF、存入数据库。
        :param url: 新闻页面的URL。
        :param category: 新闻所属的栏目（类别）。
        :param list_title: 从列表页获取的标题（可选），如果提供则优先使用。
        """
        try:
            # 检查是否为微信公众号URL，使用专门的提取器
            if WECHAT_EXTRACTOR_AVAILABLE and 'mp.weixin.qq.com' in url:
                print(f"检测到微信公众号URL，使用专门的提取器: {url}")
                logging.info(f"检测到微信公众号URL，使用专门的提取器: {url}")
                return Cl._handle_wechat_content(url, category)

            # 获取新闻页面内容
            response = safe_get(url, timeout=30)
            response.raise_for_status()  # 检查请求是否成功
            response.encoding = response.apparent_encoding  # 自动检测编码
            html = etree.HTML(response.text)

            # 使用传入的 category 参数，不再通过 XPath 提取
            category = category.strip()
            category = re.sub(r'[\\/*?:"<>|]', "", category)  # 清洗非法字符

            # ID由数据库自动生成，不需要手动设置

            # 获取标题 - 优先使用列表页传递的标题
            title = ""

            # 检查是否需要使用列表页标题
            special_handling = config_manager.get_special_handling(category)
            use_list_title = special_handling.get('use_list_title', False) if special_handling else False

            if use_list_title and list_title:
                title = list_title.strip()
                print(f"使用列表页标题: {title}")
            else:
                # 从详情页获取标题
                title_xpaths = config_manager.get_xpath_config(category, 'title_xpath')
                if title_xpaths:
                    for xpath in title_xpaths:
                        title_result = html.xpath(xpath)
                        if title_result:
                            title = title_result[0].strip()
                            print(f"使用xpath '{xpath}' 获取到标题: {title}")
                            break

                    if not title:
                        print(f"所有标题xpath都未获取到结果: {title_xpaths}")
                else:
                    print(f"分类 '{category}' 没有配置标题xpath")
                    title = ""

            # 获取元数据（来源、作者、日期等）
            metadata = {}
            metadata_xpath = special_handling.get('metadata_xpath', None) if special_handling else None

            if metadata_xpath:
                print(f"获取元数据，XPath: {metadata_xpath}")
                metadata_result = html.xpath(metadata_xpath + '//text()')
                if metadata_result:
                    # 合并所有文本
                    metadata_text = ''.join(metadata_result).strip()
                    print(f"原始元数据: {metadata_text}")

                    # 解析元数据
                    # 格式示例：来源：双流新型产业学院管委会  作者:龚悦日期：2025-11-11    审核：王强、新闻中心     点击：...

                    # 提取来源
                    source_match = re.search(r'来源[：:]\s*([^\s作]+)', metadata_text)
                    if source_match:
                        metadata['source'] = source_match.group(1).strip()

                    # 提取作者
                    author_match = re.search(r'作者[：:]\s*([^\s日]+)', metadata_text)
                    if author_match:
                        metadata['author'] = author_match.group(1).strip()

                    # 提取日期
                    date_match = re.search(r'日期[：:]\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})', metadata_text)
                    if date_match:
                        metadata['date'] = date_match.group(1).replace('/', '-')

                    # 提取审核人
                    audit_match = re.search(r'审核[：:]\s*([^点]+)', metadata_text)
                    if audit_match:
                        metadata['audit'] = audit_match.group(1).strip()

                    print(f"解析后的元数据: {metadata}")

            # 获取作者和发布时间 - 使用数据库配置（作为后备）
            print("开始获取作者和发布时间...")

            # 获取日期xpath配置
            date_xpaths = config_manager.get_xpath_config(category, 'date_xpath')
            author_xpaths = config_manager.get_xpath_config(category, 'author_xpath')

            author = metadata.get('author', '')
            date = metadata.get('date', '')

            # 获取日期 - 只有在元数据中未获取到时才使用xpath
            if not date and date_xpaths:
                for xpath in date_xpaths:
                    date_result = html.xpath(xpath)
                    if date_result:
                        date_text = date_result[0].strip()
                        # 提取日期信息
                        date_match = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{2}:\d{2}:\d{2})?)', date_text)
                        if date_match:
                            date = date_match.group(1).replace('/', '-')
                            print(f"使用xpath '{xpath}' 获取到日期: {date}")
                            break

            # 获取作者 - 只有在元数据中未获取到时才使用xpath
            if not author and author_xpaths:
                for xpath in author_xpaths:
                    author_result = html.xpath(xpath)
                    if author_result:
                        # 检查结果类型，如果是Element对象，获取其文本内容
                        if hasattr(author_result[0], 'text'):
                            author_text = author_result[0].text or ""
                        elif hasattr(author_result[0], 'strip'):
                            author_text = author_result[0].strip()
                        else:
                            author_text = str(author_result[0]).strip()

                        print(f"获取到作者原始文本: '{author_text}'")

                        # 提取作者信息
                        author_match = re.search(r'作者：([^发布时间\n\r]*?)(?=\s*发布时间|$)', author_text)
                        if author_match:
                            author = author_match.group(1).strip()
                            print(f"使用xpath '{xpath}' 获取到作者: {author}")
                            break
                        else:
                            # 如果没有"作者："前缀，直接使用文本
                            if '发布时间' not in author_text:
                                author = author_text
                                print(f"使用xpath '{xpath}' 获取到作者（无前缀）: {author}")
                                break

            # 如果数据库配置没有获取到，尝试使用原有逻辑作为后备
            if not date and not author:
                rq_djs_info = html.xpath('//*[@class="rq_djs"]//span/text()')
                print(f"后备方案 - 获取到的rq_djs信息: {rq_djs_info}")

                if rq_djs_info:
                    # 将所有文本合并
                    full_text = ''.join(rq_djs_info)
                    print(f"合并后的文本: '{full_text}'")

                    # 使用正则表达式提取作者
                    author_match = re.search(r'作者：([^发布时间]*?)(?=\s*发布时间：|$)', full_text)
                    if author_match:
                        author = author_match.group(1).strip()
                        print(f"提取到的作者: '{author}'")

                    # 使用正则表达式提取发布时间 - 支持多种格式
                    # 先尝试完整的日期时间格式
                    date_match = re.search(r'发布时间：(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', full_text)
                    if date_match:
                        date = date_match.group(1).strip()
                        print(f"提取到的完整发布时间: '{date}'")
                    else:
                        # 尝试只有日期的格式
                        date_match = re.search(r'发布时间：(\d{4}-\d{2}-\d{2})', full_text)
                        if date_match:
                            date = date_match.group(1).strip()
                            print(f"提取到的日期发布时间: '{date}'")

                    # 如果没有匹配到，尝试更宽松的匹配
                    if not author:
                        # 更精确的宽松匹配，避免匹配到"发布时间"
                        author_match = re.search(r'作者：([^发布时间\n\r]*?)(?=\s*发布时间|$)', full_text)
                        if author_match:
                            author_candidate = author_match.group(1).strip()
                            # 确保作者不为空且不包含"发布时间"
                            if author_candidate and '发布时间' not in author_candidate:
                                author = author_candidate
                                print(f"宽松匹配到的作者: '{author}'")
                            else:
                                print(f"宽松匹配的作者无效: '{author_candidate}'，保持为空")

                    if not date:
                        # 更精确的宽松匹配，避免匹配到"浏览："
                        date_match = re.search(r'发布时间：([^浏览\n\r]*?)(?=\s*浏览|$)', full_text)
                        if date_match:
                            date = date_match.group(1).strip()
                            print(f"宽松匹配到的发布时间: '{date}'")

            # 处理作者字段中的冒号：如果有冒号，只取最后一个冒号后面的内容
            if author and ':' in author:
                original_author = author
                author = author.split(':')[-1].strip()
                print(f"作者字段包含冒号，提取最后部分: '{original_author}' → '{author}'")

            print(f"最终作者: '{author}'")
            print(f"最终发布时间: '{date}'")
            print("继续处理正文内容...")

            # 保留原有的备用获取方式（已禁用，使用新的统一获取方式）
            # source = html.xpath('//div[@class="article"]//span[@class="arti-update"]/text()')
            if False:  # source:
                # 假设获取到的内容是“作者： 发表时间：2025-01-02 浏览量:355”，我们需要提取作者和发布时间
                source_text = source[0].strip()
                # 获取作者
                author = source_text.split('发表时间')[0].replace('作者：', '').strip() if '作者：' in source_text else ""
                # 获取发布时间
                date = source_text.split('浏览量')[0].split('发表时间：')[
                    -1].strip() if '发表时间：' in source_text else ""
            else:
                # 如果没能获取到内容，尝试其他方式（已禁用，使用新的统一获取方式）
                # author = html.xpath('//*[@id="js_name"]/text()')
                # author = author[0].strip() if author else ""

                # date = html.xpath('string(//*[@id="publish_time"])').strip()
                # date = date if isinstance(date, str) else ""
                pass  # 不再重新赋值author和date

            # 获取正文内容 - 使用数据库配置
            print("开始获取正文内容...")
            content_xpaths = config_manager.get_content_xpaths(category)
            text = []

            if content_xpaths:
                for i, xpath in enumerate(content_xpaths, 1):
                    xpath = xpath.strip()
                    text = html.xpath(xpath)
                    print(f"方法{i} - {xpath}: {len(text) if text else 0} 个文本节点")
                    if text:
                        print(f"使用xpath '{xpath}' 成功获取到内容")
                        break
            else:
                print(f"分类 '{category}' 没有配置内容xpath")
                text = []

            # 过滤和清理文本内容
            original_text_count = len([t for t in text if t.strip()]) if text else 0
            filtered_text_parts = []
            for t in text:
                if t.strip():
                    clean_text = t.strip()
                    # 过滤掉JavaScript代码，如showVsbVideo等
                    if not (clean_text.startswith('showVsbVideo(') or
                           'showVsbVideo(' in clean_text or
                           clean_text.startswith('function ') or
                           clean_text.startswith('var ') or
                           clean_text.startswith('document.') or
                           clean_text.startswith('window.') or
                           clean_text.startswith('$(') or
                           clean_text.startswith('jQuery')):
                        filtered_text_parts.append(clean_text)

            text = "".join(filtered_text_parts)
            print(f"过滤前文本片段数: {original_text_count}")
            print(f"过滤后正文长度: {len(text)} 字符")
            if len(text) < 200:  # 如果文本很短，显示内容用于调试
                print(f"正文内容预览: '{text[:100]}...'")

            # 如果过滤后为空，就保持为空，不再尝试获取额外内容
            if len(text) == 0:
                print("过滤后内容为空，保持为空")

            # 创建保存路径
            base_path = "/archive-file/upload/cuit"  # Linux路径格式
            category_path = os.path.join(base_path, category)
            os.makedirs(category_path, exist_ok=True)

            # 优先使用元数据中的信息
            final_date = metadata.get('date', date) if metadata else date
            final_author = metadata.get('author', author) if metadata else author
            final_source = metadata.get('source', '') if metadata else ''
            final_audit = metadata.get('audit', '') if metadata else ''

            # 获取base_domain用于网站域名字段
            base_domain = config_manager.get_base_domain(category)
            if not base_domain:
                base_domain = "https://www.cuit.edu.cn"

            dict_ = {
                "网站名称": "成都信息工程大学",
                "网站域名": base_domain,  # 只是域名，不是完整URL
                "网址": url,  # 完整的新闻URL
                "所属栏目": category,
                "标题": title,
                "发布时间": final_date,
                "来源": final_source,
                "作者": final_author,
                "审核": final_audit,
                "具体新闻内容": text,
                "采集人": "档案馆"
            }

            # 图片处理
            image_path_list = Cl._save_images(html, category_path, title, category)

            # 视频处理
            video_path_list = Cl._save_videos(html, category_path, title, category)

            # 生成 PDF
            pdf_path = Cl._generate_pdf(url, category_path, title)
            if pdf_path:
                logging.info(f"PDF 生成成功: {pdf_path}")
            else:
                logging.error("PDF 生成失败")

            # 存储到数据库
            pdf_list = [{"{}.pdf".format(title): pdf_path}] if pdf_path else []

            # 使用新的数据库插入方法，传递标题和URL
            print(f"📝 准备插入数据库: {title}")
            print(f"   URL: {url}")
            print(f"   分类: {category}")

            # 检查URL是否已存在，避免重复插入
            if not SqlClient.Cs.check_url_exists(url):
                sql = SqlClient.Cs.ins(
                    title=title,
                    url=url,
                    dict_=json.dumps(dict_, ensure_ascii=False),
                    image_path_list=image_path_list,
                    video_path_list=video_path_list,
                    pdf_list=pdf_list,
                    info="{}",
                    is_valid_link=None  # 有效链接，设为None
                )
                if sql:
                    logging.info(f"存入数据库成功：{sql}")
                    print(f"✅ 有效链接存入数据库成功: {title}")
                else:
                    print(f"❌ 数据库插入失败: {url}")
            else:
                print(f"⚠️ URL已存在于数据库中，跳过插入: {url}")

            # 打印采集到的信息用于测试
            print(f"标题: {title}")
            print(f"发布时间: {final_date}")
            print(f"来源: {final_source}")
            print(f"作者: {final_author}")
            print(f"审核: {final_audit}")
            print(f"正文长度: {len(text)} 字符")
            print(f"图片数量: {len(image_path_list)}")
            print(f"视频数量: {len(video_path_list)}")
            if image_path_list:
                print(f"图片列表: {image_path_list}")
            if video_path_list:
                print(f"视频列表: {video_path_list}")

            # 打印JSON格式
            print("\n" + "=" * 60)
            print("JSON格式预览:")
            print("=" * 60)
            print(json.dumps(dict_, ensure_ascii=False, indent=4))
            print("=" * 60 + "\n")

            # 保存为 JSON 文件
            json_path = os.path.join(category_path, f"{title}.json")
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(dict_, f, ensure_ascii=False, indent=4)
            logging.info(f"新闻详情保存成功: {json_path}")

            # 返回采集到的数据字典
            return dict_

        except Exception as e:
            print(f"❌ 抓取新闻详情失败，错误信息：{e}")
            logging.error(f"抓取新闻详情失败，错误信息：{e}")
            import traceback
            print(f"详细错误信息：{traceback.format_exc()}")
            return None

    @staticmethod
    def _save_images(html, save_dir, title, category):
        """保存图片，每张图片使用标题+序号作为键，哈希值作为文件名"""
        # 使用数据库配置获取图片xpath
        image_xpaths = config_manager.get_xpath_config(category, 'image_xpath')
        image_url_list = []

        if image_xpaths:
            for xpath in image_xpaths:
                image_url_list = html.xpath(xpath)
                print(f"使用图片xpath '{xpath}' - 找到 {len(image_url_list)} 张图片")
                if image_url_list:
                    print(f"图片URL示例: {image_url_list[0] if image_url_list else '无'}")
                    break
        else:
            print(f"分类 '{category}' 没有配置图片xpath")
            image_url_list = []

        image_path_list = []

        # 清洗标题，去除非法字符
        clean_title = re.sub(r'[\\/*?:"<>|]', "", title)

        for idx, image_url in enumerate(image_url_list):
            try:
                # 根据配置拼接完整图片 URL
                base_domain = config_manager.get_base_domain(category)
                if base_domain:
                    full_image_url = urljoin(base_domain, image_url)
                    print(f"{category}图片URL: {image_url} -> {full_image_url}")
                else:
                    # 如果没有配置基础域名，使用默认域名
                    full_image_url = urljoin('https://www.cuit.edu.cn', image_url)
                    print(f"使用默认域名拼接图片URL: {image_url} -> {full_image_url}")
                # 图片下载使用更长的超时时间和更多重试次数
                response = safe_get(full_image_url, max_retries=5, timeout=60)
                response.raise_for_status()

                # 判断是否为图片内容
                if 'image' in response.headers.get('Content-Type', ''):
                    # 获取图片格式
                    content_type = response.headers.get('Content-Type', '')
                    if 'jpeg' in content_type or 'jpg' in content_type:
                        ext = '.jpg'
                    elif 'png' in content_type:
                        ext = '.png'
                    elif 'gif' in content_type:
                        ext = '.gif'
                    else:
                        ext = '.jpg'  # 默认使用 .jpg

                    # 生成哈希文件名
                    file_hash = hashlib.md5(full_image_url.encode('utf-8')).hexdigest()
                    image_name = f"{file_hash}{ext}"
                    image_path = os.path.join(save_dir, image_name)

                    # 保存图片
                    with open(image_path, 'wb') as f:
                        f.write(response.content)
                    logging.info(f"图片保存成功: {image_path}")

                    # 生成输出格式：标题 + 序号为键，文件路径为值
                    output_key = f"{clean_title}{idx + 1}{ext}"  # 标题 + 序号 + 图片格式
                    image_path_list.append({output_key: image_path})
                else:
                    logging.warning(f"下载失败，非图片内容: {full_image_url}")
            except RequestException as e:
                logging.error(f"图片下载请求错误: {e} - URL: {image_url}")
            except Exception as e:
                logging.error(f"图片保存错误: {e} - URL: {image_url}")
        return image_path_list

    @staticmethod
    def _generate_pdf(url, save_dir, title):
        """根据新闻内容生成 PDF 文件 - 统一使用ImprovedPDFGenerator"""
        try:
            if PDF_GENERATOR_AVAILABLE:
                print(f"使用统一PDF生成器: {url}")
                logging.info(f"使用统一PDF生成器: {url}")

                # 创建PDF生成器实例
                pdf_generator = ImprovedPDFGenerator()

                # 异步生成PDF
                try:
                    pdf_path = asyncio.run(pdf_generator.generate_pdf(url, save_dir, title))
                    if pdf_path:
                        logging.info(f"PDF生成成功: {pdf_path}")
                        print(f"PDF生成成功: {pdf_path}")
                        return pdf_path
                    else:
                        logging.error("PDF生成失败")
                        print("PDF生成失败")
                        return None
                except Exception as e:
                    print(f"PDF生成异常: {e}")
                    logging.error(f"PDF生成异常: {e}")
                    return None
            else:
                print("PDF生成器不可用")
                logging.error("PDF生成器不可用")
                return None

        except Exception as e:
            logging.error(f"PDF 生成失败: {e}")
            print(f"PDF 生成失败: {e}")
            return None

    @staticmethod
    def _save_videos(html, save_dir, title, category):
        """保存视频，每个视频使用标题+序号作为键，哈希值作为文件名"""
        # 使用配置获取视频xpath
        video_xpaths = config_manager.get_xpath_config(category, 'video_xpath')
        video_url_list = []

        print("开始查找视频...")

        # 先检查页面中是否包含视频相关的文本
        page_text = etree.tostring(html, encoding='unicode', method='html')
        if '_vsb_player' in page_text:
            print("页面包含_vsb_player")
        if 'video' in page_text.lower():
            print("页面包含video标签")
        if '.mp4' in page_text:
            print("页面包含.mp4文件")

        # 使用配置的xpath方法
        if video_xpaths:
            for i, xpath in enumerate(video_xpaths, 1):
                video_url_list = html.xpath(xpath)
                print(f"方法{i} - {xpath}: {video_url_list}")
                if video_url_list:
                    print(f"使用视频xpath '{xpath}' 成功获取到 {len(video_url_list)} 个视频")
                    break
        else:
            print(f"分类 '{category}' 没有配置视频xpath")

        # 如果还是找不到，尝试从页面文本中提取.mp4链接
        if not video_url_list:
            import re
            # 优化正则表达式：只匹配以 / 或 http 开头的有效路径
            # 匹配 showVsbVideo("/__local/...mp4") 这种格式
            mp4_matches = re.findall(r'["\']([/](?:__local|[^"\'<>]+)\.mp4[^"\'<>]*)["\']', page_text)
            if mp4_matches:
                print(f"从页面文本中找到mp4链接: {mp4_matches}")
                # 过滤掉无效的URL（包含HTML标签的）
                valid_urls = [url for url in mp4_matches if not any(char in url for char in ['<', '>', '【', '】'])]
                # 去除重复的URL
                video_url_list = list(set(valid_urls))
                print(f"过滤并去重后的mp4链接: {video_url_list}")

        print(f"最终视频URL列表: {video_url_list}")

        # 打印对应的xpath调试信息
        if video_url_list:
            print("尝试用xpath直接获取这些视频:")
            for video_url in video_url_list:
                # 尝试找到包含这个URL的xpath
                xpath_results = html.xpath(f'//source[@src="{video_url}"]')
                if xpath_results:
                    print(f"  视频 {video_url} 对应的xpath: //source[@src=\"{video_url}\"]")
                else:
                    # 尝试查找父元素
                    xpath_results = html.xpath(f'//*[contains(@src, "{video_url.split("/")[-1].split("?")[0]}")]')
                    if xpath_results:
                        print(f"  视频 {video_url} 可能的xpath: //*[contains(@src, \"{video_url.split('/')[-1].split('?')[0]}\")]")

        video_path_list = []

        # 清洗标题，去除非法字符
        clean_title = re.sub(r'[\\/*?:"<>|]', "", title)

        for idx, video_url in enumerate(video_url_list):
            try:
                # 拼接完整视频 URL - 使用配置的base_domain
                if not video_url.startswith('http'):
                    base_domain = config_manager.get_base_domain(category)
                    if base_domain:
                        full_video_url = urljoin(base_domain, video_url)
                        print(f"{category}视频URL: {video_url} -> {full_video_url}")
                    else:
                        # 如果没有配置基础域名，使用默认域名
                        full_video_url = urljoin('https://www.cuit.edu.cn', video_url)
                        print(f"使用默认域名拼接视频URL: {video_url} -> {full_video_url}")
                else:
                    full_video_url = video_url
                    print(f"视频URL已是完整路径: {full_video_url}")

                print(f"开始下载视频: {full_video_url}")

                # 增加超时时间和重试机制 - 视频文件通常很大，需要更长的超时时间
                max_retries = 5  # 增加到5次重试
                response = None
                for retry in range(max_retries):
                    try:
                        print(f"尝试下载 (第{retry+1}/{max_retries}次)...")
                        # 超时时间设置为600秒（10分钟），视频文件可能很大
                        response = safe_get(full_video_url, timeout=600, stream=True, max_retries=1)
                        response.raise_for_status()
                        print(f"✅ 连接成功，开始下载...")
                        break  # 如果成功，跳出重试循环
                    except requests.exceptions.Timeout:
                        print(f"⏱️ 下载超时 (第{retry+1}/{max_retries}次)，视频文件较大，正在重试...")
                        if retry < max_retries - 1:
                            import time
                            wait_time = (retry + 1) * 10  # 递增等待时间：10秒、20秒、30秒...
                            print(f"等待 {wait_time} 秒后重试...")
                            time.sleep(wait_time)
                        else:
                            print("❌ 下载失败：多次超时，跳过此视频")
                            break  # 跳出重试循环
                    except Exception as e:
                        print(f"❌ 下载错误 (第{retry+1}/{max_retries}次): {e}")
                        if retry < max_retries - 1:
                            import time
                            wait_time = (retry + 1) * 5
                            print(f"等待 {wait_time} 秒后重试...")
                            time.sleep(wait_time)
                        else:
                            print("❌ 下载失败：多次错误，跳过此视频")
                            break  # 跳出重试循环

                # 检查是否成功获取到response
                if response is None:
                    print("❌ 下载失败，跳过此视频")
                    continue

                # 判断是否为视频内容
                content_type = response.headers.get('Content-Type', '')
                print(f"Content-Type: {content_type}")
                if 'video' in content_type or video_url.endswith(('.mp4', '.avi', '.mov', '.wmv', '.flv')):
                    # 获取视频格式
                    if '.mp4' in video_url:
                        ext = '.mp4'
                    elif '.avi' in video_url:
                        ext = '.avi'
                    elif '.mov' in video_url:
                        ext = '.mov'
                    elif '.wmv' in video_url:
                        ext = '.wmv'
                    elif '.flv' in video_url:
                        ext = '.flv'
                    else:
                        ext = '.mp4'  # 默认使用 .mp4

                    # 生成哈希文件名
                    file_hash = hashlib.md5(full_video_url.encode('utf-8')).hexdigest()
                    video_name = f"{file_hash}{ext}"
                    video_path = os.path.join(save_dir, video_name)

                    # 获取文件大小信息
                    content_length = response.headers.get('Content-Length')
                    if content_length:
                        file_size_mb = int(content_length) / (1024 * 1024)
                        print(f"视频文件大小: {file_size_mb:.2f} MB")

                    # 保存视频
                    print("📥 开始保存视频文件...")
                    downloaded_size = 0
                    last_progress = 0
                    with open(video_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded_size += len(chunk)
                                # 每下载5MB显示一次进度
                                current_mb = downloaded_size / (1024 * 1024)
                                if int(current_mb / 5) > last_progress:
                                    last_progress = int(current_mb / 5)
                                    if content_length:
                                        progress = (downloaded_size / int(content_length)) * 100
                                        print(f"📊 下载进度: {current_mb:.2f} MB / {file_size_mb:.2f} MB ({progress:.1f}%)")
                                    else:
                                        print(f"📊 已下载: {current_mb:.2f} MB")

                    final_size_mb = downloaded_size / (1024 * 1024)
                    logging.info(f"视频保存成功: {video_path}, 大小: {final_size_mb:.2f} MB")
                    print(f"✅ 视频保存成功: {video_path}")
                    print(f"📦 文件大小: {final_size_mb:.2f} MB")

                    # 生成输出格式：标题 + 序号为键，文件路径为值
                    output_key = f"{clean_title}{idx + 1}{ext}"  # 标题 + 序号 + 视频格式
                    video_path_list.append({output_key: video_path})
                else:
                    logging.warning(f"下载失败，非视频内容: {full_video_url}")
                    print(f"下载失败，非视频内容: {full_video_url}")
            except RequestException as e:
                logging.error(f"视频下载请求错误: {e} - URL: {video_url}")
                print(f"视频下载请求错误: {e} - URL: {video_url}")
            except Exception as e:
                logging.error(f"视频保存错误: {e} - URL: {video_url}")
                print(f"视频保存错误: {e} - URL: {video_url}")

        return video_path_list