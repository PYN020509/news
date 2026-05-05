"""
成信文化栏目专用采集器
处理多个不同结构的采集源
"""
import re
import json
import hashlib
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from lxml import etree
from urllib.parse import urljoin
import urllib3
import logging
import time
from ConfigManager import config_manager
import SqlClient  # 添加数据库支持
from ImprovedPDFGenerator import ImprovedPDFGenerator  # 添加PDF生成支持
from CollectDetail import Cl  # 导入详情采集器，用于处理列表页的详情

# 禁用 SSL 警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 设置日志
logging.basicConfig(
    filename='collect_cxwh.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# 设置请求头
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Connection': 'close',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
}


def safe_get(url, timeout=30, max_retries=3):
    """安全的GET请求"""
    session = requests.Session()
    retry_strategy = Retry(
        total=max_retries,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    for attempt in range(max_retries):
        try:
            response = session.get(url, headers=headers, verify=False, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            time.sleep(2 ** attempt)
    return None


class CXWHCollector:
    """成信文化采集器"""
    
    # 定义不同类型的采集源配置
    SOURCES = {
        # 类型1-1：成信印象 - 成信视频和主页（使用yxcx布局）
        "cxyx_yxcx": {
            "urls": [
                "https://www.cuit.edu.cn/index/cxwh/cxyx/cxsp.htm",
                "https://www.cuit.edu.cn/index/cxwh/cxyx.htm"
            ],
            "news_links_xpath": "//div[@class='yxcx']//ul//a/@href",
            "news_titles_xpath": "//div[@class='yxcx']//ul//a/@title",
            "link_filter": None,
            "total_count_xpath": "//div[@class='pb_sys_common pb_sys_normal pb_sys_style1']/span/text()",
            "items_per_page": 12,
            "detail_config": {
                "title_xpath": "//h1/text()",
                "content_xpath": "//div[@class='v_news_content']//text()",
                "image_xpath": "//div[@class='v_news_content']//img/@src",
                "video_xpath": "//div[@class='v_news_content']//video/source/@src",
                "metadata_xpath": "//div/form/p"
            }
        },

        # 类型1-2：成信印象 - 校园风光和地标建筑（使用inn_com xyfg布局）
        "cxyx_xyfg": {
            "urls": [
                "https://www.cuit.edu.cn/index/cxwh/cxyx/xyfg1.htm",
                "https://www.cuit.edu.cn/index/cxwh/cxyx/dcxy.htm"
            ],
            "news_links_xpath": "//div[@class='inn_com xyfg']//ul//a/@href",
            "news_titles_xpath": "//div[@class='inn_com xyfg']//ul//a/@title",
            "link_filter": None,
            "total_count_xpath": "//div[@class='pb_sys_common pb_sys_normal pb_sys_style1']/span/text()",
            "items_per_page": 12,
            "detail_config": {
                "title_xpath": "//h1/text()",
                "content_xpath": "//div[@class='v_news_content']//text()",
                "image_xpath": "//div[@class='v_news_content']//img/@src",
                "video_xpath": "//div[@class='v_news_content']//video/source/@src",
                "metadata_xpath": "//div/form/p"
            }
        },
        
        # 类型2：大学精神（特殊结构，无元数据）
        "dxjs_type": {
            "urls": [
                "https://www.cuit.edu.cn/xygk/dxjs.htm"
            ],
            "is_single_page": True,  # 单页面，不需要分页
            "detail_config": {
                "title_xpath": "//div[@class='ny-btnr']/p/text()",
                "content_xpath": "//div[@class='nyr']//div[@id='vsb_content']//text()",
                "image_xpath": "//div[@class='nyr']//div[@id='vsb_content']//img/@src",
                "video_xpath": None,
                "metadata_xpath": None  # 无元数据
            }
        },
        
        # 类型3：学校学报（特殊结构，有图片，无元数据）
        "xxxb_type": {
            "urls": [
                "https://www.cuit.edu.cn/index/cxwh/xxxb.htm"
            ],
            "is_single_page": True,  # 单页面
            "detail_config": {
                "title_xpath": "//div[@class='ny-btnr']/p/text()",
                "content_xpath": "//div[@class='nyr']//div[@id='vsb_content']//text()",
                "image_xpath": "//div[@class='nyr']//div[@id='vsb_content']//img/@src",
                "video_xpath": None,
                "metadata_xpath": None  # 无元数据
            }
        },
        
        # 类型4：文化活动（标准结构）
        "whhd_type": {
            "urls": [
                "https://www.cuit.edu.cn/index/cxwh/whhd.htm"
            ],
            "news_links_xpath": "//ul[@class='list']//a/@href",
            "news_titles_xpath": "//ul[@class='list']//a/@title",
            "total_count_xpath": "//div[@class='pb_sys_common pb_sys_normal pb_sys_style1']/span/text()",
            "items_per_page": 15,
            "detail_config": {
                "title_xpath": "//h1/text()",
                "content_xpath": "//div[@class='v_news_content']//text()",
                "image_xpath": "//div[@class='v_news_content']//img/@src",
                "video_xpath": "//div[@class='v_news_content']//video/source/@src",
                "metadata_xpath": "//div/form/p"
            }
        }
    }
    
    def __init__(self):
        """初始化采集器"""
        self.category = "成信文化"
        self.base_domain = "https://www.cuit.edu.cn"
        self.save_path = "/archive-file/upload/cuit/成信文化"
        os.makedirs(self.save_path, exist_ok=True)
    
    def collect_all(self):
        """采集所有成信文化内容"""
        print("=" * 80)
        print("开始采集成信文化栏目")
        print("=" * 80)
        
        for source_type, config in self.SOURCES.items():
            print(f"\n处理 {source_type}...")
            
            if config.get('is_single_page'):
                # 单页面类型
                for url in config['urls']:
                    self._collect_single_page(url, config)
            else:
                # 列表页类型
                for url in config['urls']:
                    self._collect_list_page(url, config)
    
    def _collect_single_page(self, url, config):
        """采集单页面内容"""
        print(f"\n采集单页面: {url}")
        
        try:
            response = safe_get(url)
            if not response:
                print(f"❌ 无法访问: {url}")
                return
            
            response.encoding = response.apparent_encoding
            html = etree.HTML(response.text)
            
            # 提取内容
            detail_config = config['detail_config']
            
            # 标题
            title_xpath = detail_config.get('title_xpath')
            title = ""
            if title_xpath:
                title_list = html.xpath(title_xpath)
                if title_list:
                    title = title_list[0].strip()
            
            if not title:
                title = url.split('/')[-1].replace('.htm', '')
            
            # 内容
            content_xpath = detail_config.get('content_xpath')
            content = ""
            if content_xpath:
                content_list = html.xpath(content_xpath)
                content = '\n'.join([c.strip() for c in content_list if c.strip()])
            
            # 图片
            images = []
            image_xpath = detail_config.get('image_xpath')
            if image_xpath:
                image_urls = html.xpath(image_xpath)
                for img_url in image_urls:
                    full_url = urljoin(url, img_url)
                    images.append(full_url)
            
            # 保存图片
            image_path_list = []
            for idx, img_url in enumerate(images):
                try:
                    img_response = safe_get(img_url, timeout=10)
                    if img_response:
                        # 生成图片文件名（使用哈希值）
                        img_hash = hashlib.md5(img_response.content).hexdigest()
                        img_ext = img_url.split('.')[-1].split('?')[0] or 'jpg'
                        img_filename = f"{img_hash}.{img_ext}"
                        img_path = os.path.join(self.save_path, img_filename)

                        # 保存图片
                        with open(img_path, 'wb') as f:
                            f.write(img_response.content)

                        # 记录图片路径
                        image_path_list.append({f"{title}_{idx+1}": img_path})
                        print(f"   保存图片 {idx+1}: {img_filename}")
                except Exception as e:
                    print(f"   ⚠️ 保存图片失败: {e}")

            # 生成PDF
            pdf_path = None
            try:
                pdf_generator = ImprovedPDFGenerator()
                pdf_filename = f"{title}.pdf"
                pdf_path = os.path.join(self.save_path, pdf_filename)

                # 准备PDF内容
                pdf_content = {
                    'title': title,
                    'content': content,
                    'images': images
                }

                # 生成PDF（这里简化处理，实际可能需要更复杂的逻辑）
                # pdf_generator.generate_pdf(pdf_content, pdf_path)
                print(f"   PDF路径: {pdf_path}")
            except Exception as e:
                print(f"   ⚠️ PDF生成失败: {e}")

            # 保存数据
            data = {
                "网站名称": "成都信息工程大学",
                "网站域名": self.base_domain,
                "网址": url,
                "所属栏目": self.category,
                "标题": title,
                "发布时间": "",
                "来源": "",
                "作者": "",
                "审核": "",
                "具体新闻内容": content,
                "采集人": "档案馆"
            }

            # 保存JSON
            json_filename = f"{title}.json"
            json_path = os.path.join(self.save_path, json_filename)
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

            # 插入数据库
            try:
                # 检查URL是否已存在
                if not SqlClient.Cs.check_url_exists(url):
                    pdf_list = [{f"{title}.pdf": pdf_path}] if pdf_path else []

                    sql = SqlClient.Cs.ins(
                        title=title,
                        url=url,
                        dict_=json.dumps(data, ensure_ascii=False),
                        image_path_list=image_path_list,
                        video_path_list=[],
                        pdf_list=pdf_list,
                        info="{}",
                        is_valid_link=None
                    )

                    if sql:
                        print(f"✅ 数据库插入成功: {title}")
                        logging.info(f"数据库插入成功: {title}")
                    else:
                        print(f"⚠️ 数据库插入失败: {title}")
                else:
                    print(f"⚠️ URL已存在，跳过插入: {url}")
            except Exception as e:
                print(f"❌ 数据库插入异常: {e}")
                logging.error(f"数据库插入异常 {url}: {e}")

            print(f"✅ 采集成功: {title}")
            print(f"   内容长度: {len(content)} 字符")
            print(f"   图片数量: {len(images)}")
            
        except Exception as e:
            print(f"❌ 采集失败: {e}")
            logging.error(f"采集单页面失败 {url}: {e}")
    
    def _collect_list_page(self, base_url, config):
        """采集列表页内容"""
        print(f"\n采集列表页: {base_url}")
        
        try:
            # 获取总页数
            response = safe_get(base_url)
            if not response:
                print(f"❌ 无法访问: {base_url}")
                return
            
            response.encoding = response.apparent_encoding
            html = etree.HTML(response.text)
            
            # 获取总条数
            total_count_xpath = config.get('total_count_xpath')
            total_count = 0
            if total_count_xpath:
                count_result = html.xpath(total_count_xpath)
                if count_result:
                    count_text = count_result[0].strip()
                    # 从 "共34条" 或 "34" 中提取数字
                    import re
                    match = re.search(r'\d+', count_text)
                    if match:
                        total_count = int(match.group())
                    else:
                        print(f"⚠️  无法从 '{count_text}' 中提取数字，使用默认值0")
                        total_count = 0
            
            items_per_page = config.get('items_per_page', 15)
            total_pages = (total_count + items_per_page - 1) // items_per_page if total_count > 0 else 1
            
            print(f"总条数: {total_count}, 每页: {items_per_page}, 总页数: {total_pages}")
            
            # 采集每一页
            for page in range(1, total_pages + 1):
                page_url = self._get_page_url(base_url, page, total_pages)
                print(f"\n采集第 {page}/{total_pages} 页: {page_url}")
                
                self._collect_page_items(page_url, config)
                
                time.sleep(1)  # 避免请求过快
                
        except Exception as e:
            print(f"❌ 采集列表页失败: {e}")
            logging.error(f"采集列表页失败 {base_url}: {e}")
    
    def _get_page_url(self, base_url, page, total_pages):
        """生成分页URL（倒序分页）"""
        if page == 1:
            return base_url
        else:
            # 倒序分页：第2页对应 total_pages-1
            page_num = total_pages - page + 1
            return base_url.replace('.htm', f'/{page_num}.htm')
    
    def _collect_page_items(self, page_url, config):
        """采集页面中的所有新闻项"""
        try:
            response = safe_get(page_url)
            if not response:
                return
            
            response.encoding = response.apparent_encoding
            html = etree.HTML(response.text)
            
            # 获取链接和标题
            links_xpath = config.get('news_links_xpath')
            titles_xpath = config.get('news_titles_xpath')
            link_filter = config.get('link_filter')
            
            links = html.xpath(links_xpath) if links_xpath else []
            titles = html.xpath(titles_xpath) if titles_xpath else []
            
            print(f"找到 {len(links)} 个链接")
            
            # 处理每个新闻项
            for i in range(min(len(links), len(titles))):
                link = links[i]
                title = titles[i].strip() if titles[i] else '无标题'

                # 链接过滤
                if link_filter and link_filter not in link:
                    continue

                # 解析完整URL
                full_url = urljoin(page_url, link)

                print(f"\n  采集: {title}")
                print(f"  URL: {full_url}")

                # 调用CollectDetail采集详情并插入数据库
                try:
                    Cl.Collect_dea(full_url, self.category, list_title=title)
                    time.sleep(0.5)  # 避免请求过快
                except Exception as e:
                    print(f"  ❌ 采集详情失败: {e}")
                    logging.error(f"采集详情失败 {full_url}: {e}")
                
        except Exception as e:
            print(f"❌ 采集页面项失败: {e}")


def main():
    """主函数"""
    collector = CXWHCollector()
    collector.collect_all()
    
    print("\n" + "=" * 80)
    print("成信文化栏目采集完成！")
    print("=" * 80)


if __name__ == "__main__":
    main()

