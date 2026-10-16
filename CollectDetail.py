import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ConfigManager import config_manager
import SqlClient


logger = logging.getLogger(__name__)
headers = {"User-Agent": "GSZY-ArchiveCollector/1.0", "Accept": "application/json,text/html,*/*"}
session = requests.Session()
session.headers.update(headers)
session.verify = os.environ.get("NEWS_VERIFY_TLS", "true").lower() not in ("false", "0", "no")
adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1,
                                      status_forcelist=[429, 500, 502, 503, 504],
                                      allowed_methods=["GET", "POST"]))
session.mount("https://", adapter)
session.mount("http://", adapter)


def safe_get(url, max_retries=3, **kwargs):
    """复用原下载入口，由 HTTP 连接池统一处理短暂故障。"""
    response = session.get(url, timeout=kwargs.pop("timeout", (10, 60)), **kwargs)
    response.raise_for_status()
    return response


def _publication_date(value):
    """只接受可解析的发布日期，不能用采集日期代替。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", str(value or ""))
    if not match:
        raise ValueError("文章发布日期缺失或格式不正确")
    return date(*(int(part) for part in match.groups()))


class Cl:
    """通过站点的正式数据接口采集详情，沿用新闻表和 PDF 生成器。"""

    @staticmethod
    def Collect_dea(url, category, list_title=None, list_date=None, article_data=None,
                    start_date=None, end_date=None):
        """全部文件成功落盘后才入库，失败返回空值供上层保留重试任务。"""
        try:
            config = config_manager.get_category_config(category)
            if not config:
                raise ValueError("未配置的栏目：" + category)
            source_item = article_data or {}
            article_id = source_item.get("id")
            if not article_id:
                match = re.search(r"/detail/([^/]+)\.htm$", urlsplit(url).path)
                article_id = match.group(1) if match else None
            if not article_id:
                raise ValueError("文章编号缺失")
            response = session.post(config["detail_api"], data={"id": article_id}, timeout=(10, 30))
            response.raise_for_status()
            article = response.json()
            if not isinstance(article, dict) or not article.get("id"):
                raise ValueError("详情接口没有返回有效文章")
            if str(article["id"]) != str(article_id):
                raise ValueError("详情编号与请求编号不一致")
            published = _publication_date(article.get(config["date_field"]))
            if list_date and published != _publication_date(list_date):
                raise ValueError("列表与详情的发布日期不一致，需重新扫描")
            first = _publication_date(start_date) if start_date else None
            last = _publication_date(end_date) if end_date else None
            if (first and published < first) or (last and published > last):
                return {"skipped": True}
            title = str(article.get("title") or list_title or "").strip()
            if not title:
                raise ValueError("文章标题为空")
            content_html = str(article.get("content_html") or "")
            html = etree.HTML(content_html) if content_html.strip() else None
            attachments = Cl._attachment_items(article.get("attachments"))
            enclosure = article.get("enclosure")
            if enclosure:
                attachments.extend(Cl._attachment_items(enclosure, article.get("enclosure_name")))
            video_items = Cl._attachment_items(article.get("news_video") or article.get("notice_video"))
            if html is not None:
                for node in html.xpath("//script|//style|//noscript"):
                    node.getparent().remove(node)
                text = "\n".join(part.strip() for part in html.xpath("//body//text()") if part.strip())
            else:
                text = ""
            if not text and not attachments and not video_items and not (html is not None and html.xpath("//img|//video")):
                raise ValueError("文章正文及附件均为空")
            article_dir = (Path(os.environ.get("NEWS_DATA_DIR", "/var/lib/news/data"))
                           / os.environ.get("NEWS_SITE_ID", "gszy") / category / str(published.year)
                           / hashlib.sha256(url.encode("utf-8")).hexdigest())
            article_dir.mkdir(parents=True, exist_ok=True)
            images = Cl._save_images(html, article_dir, title, category, url) if html is not None else []
            videos = Cl._save_videos(html, article_dir, title, category, url) if html is not None else []
            if video_items:
                videos.extend(Cl._save_attachments(video_items, None, article_dir, url))
            documents = Cl._save_attachments(attachments, html, article_dir, url)
            pdf_enabled = os.environ.get("NEWS_PDF_ENABLED", "true").lower() not in ("false", "0", "no")
            if pdf_enabled:
                pdf_path = Cl._generate_pdf(url, article_dir, title)
                if not pdf_path:
                    raise RuntimeError("PDF 生成失败，文章未标记为采集成功")
                documents.insert(0, {title + ".pdf": pdf_path})
            payload = {
                "网站名称": "甘肃中医药大学新闻网", "网站域名": config["base_domain"],
                "网址": url, "所属栏目": category, "标题": title,
                "发布时间": published.isoformat(), "年度": str(published.year),
                "分类号": config["classification"], "所属栏目编号": config["category_code"],
                "来源": str(article.get("source") or ""),
                "作者": str(article.get("author") or ""), "编辑": str(article.get("editor") or ""),
                "具体新闻内容": text, "摘要": text[:500], "采集人": "档案馆",
                "采集时间": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
                "来源链接": str(article.get("news_link") or ""),
            }
            Cl._write_file(article_dir / "content.html", content_html.encode("utf-8"))
            Cl._write_file(article_dir / "article.json",
                           json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            Cl._write_file(article_dir / "source.json",
                           json.dumps(article, ensure_ascii=False, indent=2).encode("utf-8"))
            record_id = SqlClient.Cs.ins(title, url, payload, images, videos, documents, {})
            if not record_id:
                raise RuntimeError("新闻表入库失败")
            logger.info("文章已采集：%s %s %s", category, published, url)
            return payload
        except Exception:
            logger.exception("文章详情采集失败：%s %s", category, url)
            return None

    @staticmethod
    def _asset_url(value, page_url):
        """规范资源地址，站内 HTTP 资源统一使用 HTTPS。"""
        parsed = urlsplit(urljoin(page_url, str(value).strip()))
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("不支持的资源地址")
        scheme = "https" if parsed.hostname == "www.gszy.edu.cn" else parsed.scheme
        return urlunsplit((scheme, parsed.netloc, re.sub(r"/{2,}", "/", parsed.path), parsed.query, ""))

    @staticmethod
    def _write_file(path, content):
        """以同目录临时文件原子替换，避免中断后留下半份制品。"""
        temporary = Path(str(path) + ".part")
        temporary.write_bytes(content)
        temporary.replace(path)

    @staticmethod
    def _download(value, save_dir, page_url, display_name=None, expect_image=False):
        """保留资源原扩展名和显示名，校验响应后原子保存。"""
        resource_url = Cl._asset_url(value, page_url)
        path_name = Path(urlsplit(resource_url).path).name or "attachment"
        extension = Path(path_name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension):
            extension = ".bin"
        response = safe_get(resource_url, stream=True, headers={"Referer": page_url})
        try:
            content_type = response.headers.get("Content-Type", "").lower()
            if expect_image and not content_type.startswith("image/"):
                raise ValueError("图片下载返回的内容类型不正确")
            if not expect_image and ("text/html" in content_type or "application/json" in content_type):
                raise ValueError("附件下载返回了页面或错误数据")
            if expect_image and extension == ".bin":
                extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                             "image/webp": ".webp"}.get(content_type.split(";")[0], ".img")
            path = Path(save_dir) / (hashlib.sha256(resource_url.encode("utf-8")).hexdigest() + extension)
            temporary = Path(str(path) + ".part")
            size = 0
            with temporary.open("wb") as output:
                for chunk in response.iter_content(1024 * 128):
                    if chunk:
                        output.write(chunk)
                        size += len(chunk)
            if not size:
                raise ValueError("下载得到空文件")
            temporary.replace(path)
            name = str(display_name or path_name).strip()
            name = re.sub(r'[\\/\x00-\x1f]', "_", name) or path_name
            if not Path(name).suffix:
                name += extension
            return {name: str(path)}
        finally:
            response.close()

    @staticmethod
    def _save_images(html, save_dir, title, category, page_url=None):
        """下载正文图片，任何图片失败均阻止文章入库。"""
        urls = html.xpath("//img/@data-src|//img[not(@data-src)]/@src")
        return [Cl._download(value, save_dir, page_url or config_manager.get_base_domain(category),
                             title + "_" + str(index) + Path(urlsplit(value).path).suffix, True)
                for index, value in enumerate(dict.fromkeys(urls), 1) if value.strip()]

    @staticmethod
    def _save_videos(html, save_dir, title, category, page_url=None):
        """下载正文视频并保留与原爬虫一致的路径列表。"""
        urls = html.xpath("//video/@src|//video/source/@src")
        return [Cl._download(value, save_dir, page_url or config_manager.get_base_domain(category))
                for value in dict.fromkeys(urls) if value.strip()]

    @staticmethod
    def _save_attachments(attachments, html, save_dir, page_url):
        """采集接口附件和正文中的文件链接，按规范地址去重。"""
        urls = {}
        for item in attachments:
            if isinstance(item, str):
                value, name = item, None
            elif isinstance(item, dict):
                value = next((item[key] for key in ("url", "file_path", "file_url", "path", "src",
                                                   "filePath", "fileUrl", "attachmentUrl", "attachment_url")
                              if item.get(key)), None)
                name = item.get("file_name") or item.get("fileName") or item.get("name") or item.get("title")
            else:
                raise ValueError("附件条目格式不正确")
            if not value:
                raise ValueError("附件没有下载地址")
            urls[Cl._asset_url(value, page_url)] = name
        if html is not None:
            for anchor in html.xpath("//a[@href]"):
                value = anchor.get("href")
                if Path(urlsplit(value).path).suffix.lower() in {
                    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z"}:
                    urls.setdefault(Cl._asset_url(value, page_url), "".join(anchor.itertext()).strip() or None)
        return [Cl._download(value, save_dir, page_url, name) for value, name in urls.items()]

    @staticmethod
    def _attachment_items(value, display_name=None):
        """兼容接口返回的空值、资源路径及 JSON 数组，未知格式明确报错。"""
        if not value:
            return []
        if isinstance(value, str):
            value = value.strip()
            if not value or value == "null":
                return []
            if value.startswith(("[", "{")):
                value = json.loads(value)
            else:
                return [{"url": value, "name": display_name}] if display_name else [value]
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            return value
        raise ValueError("文章资源字段格式不正确")

    @staticmethod
    def _generate_pdf(url, save_dir, title):
        """延迟加载既有生成器，仅在实际采集时运行 PDF 生产流程。"""
        try:
            from ImprovedPDFGenerator import ImprovedPDFGenerator
            generator = ImprovedPDFGenerator(output_dir=str(save_dir))
            return asyncio.run(generator.generate_pdf(url, str(save_dir), title))
        except (Exception, SystemExit):
            logger.exception("PDF 生成失败：%s", url)
            return None
