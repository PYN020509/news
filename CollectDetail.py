import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ConfigManager import config_manager
import SqlClient


logger = logging.getLogger(__name__)
headers = {"User-Agent": "SMZY-ArchiveCollector/1.0", "Accept": "text/html,application/xhtml+xml,*/*"}
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
    match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", str(value or ""))
    if not match:
        raise ValueError("文章发布日期缺失或格式不正确")
    return date(*(int(part) for part in match.groups()))


class Cl:
    """复用新闻暂存、原文件下载及 PDF 流程，适配神木主站与列表直链部门详情。"""

    @staticmethod
    def _request_article(url, config):
        """仅请求所选列表引用的站内详情，拒绝跨站或详情页重定向。"""
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != urlsplit(config["base_domain"]).netloc
                or not re.fullmatch(config["detail_path_pattern"], parsed.path)):
            raise ValueError("详情地址不属于已选列表允许的站内详情")
        response = safe_get(url, headers={"Referer": config["base_url"]})
        try:
            final = urlsplit(response.url)
            if final.netloc != parsed.netloc or final.path != parsed.path:
                raise ValueError("详情被重定向至其他页面")
            return Cl._parse_article(response.content.decode("utf-8-sig"), config, url)
        finally:
            response.close()

    @staticmethod
    def _parse_article(content, config, url):
        """从当前主站和部门站的真实模板解析元数据，并在清理脚本前提取原文件。"""
        html = etree.HTML(content)
        if html is None:
            raise ValueError("详情未返回有效 HTML")
        title = ""
        for xpath in config["title_xpath"]:
            nodes = html.xpath(xpath)
            if nodes:
                if len(nodes) != 1:
                    raise ValueError("详情新闻标题不唯一")
                title = ("".join(nodes[0].itertext()) if hasattr(nodes[0], "itertext") else str(nodes[0])).strip()
                break
        if not title:
            raise ValueError("文章标题为空")
        article = {"id": Path(urlsplit(url).path).stem, "title": title, "source_html": content}
        labels = r"发布时间|发布日期|作者|来源|责任编辑|编辑"
        metadata = []
        for node in html.xpath(config["metadata_xpath"]):
            metadata.append(" ".join(part.strip() for part in node.xpath(".//text()[not(ancestor::script)]") if part.strip()))
        raw = " ".join(metadata)
        fields = {"发布时间": config["date_field"], "发布日期": config["date_field"],
                  "作者": "author", "来源": "source", "编辑": "editor", "责任编辑": "editor"}
        for name, value in re.findall(r"(" + labels + r")\s*[：:]\s*(.*?)(?=(?:" + labels + r"|点击数|访问次数)\s*[：:]|$)", raw, re.S):
            field = fields[name]
            value = re.sub(r"(?:&nbsp;?|\s)+$", "", value).strip()
            if field in article and article[field] != value:
                raise ValueError("详情元信息重复且不一致：" + name)
            article[field] = value
        article["publication_raw"] = article.get(config["date_field"], "")
        try:
            article[config["date_field"]] = _publication_date(article["publication_raw"]).isoformat()
        except ValueError as exc:
            raise ValueError("文章发布日期缺失或不可解析：" + str(article["publication_raw"])) from exc
        bodies = []
        for xpath in config["content_xpath"]:
            bodies = html.xpath(xpath)
            if bodies:
                break
        if len(bodies) != 1:
            raise ValueError("详情缺少唯一新闻正文")
        body = bodies[0]
        article["content_html"] = etree.tostring(body, encoding="unicode", method="html")
        videos, attachments = [], []
        for script in body.xpath(".//script"):
            value = script.get("vurl")
            script_text = script.text or ""
            if value and (script.get("name") == "_videourl" or re.search(r"\.(?:mp4|webm|m4v)(?:[?#]|$)", value, re.I)):
                videos.append(value)
            videos.extend(re.findall(r"showVsbVideo\(\s*['\"]([^'\"]+)['\"]", script_text, re.I))
            attachments.extend(re.findall(r"showVsbpdfIframe\(\s*['\"]([^'\"]+)['\"]", script_text, re.I))
        article["news_video"] = list(dict.fromkeys(videos))
        article["attachments"] = [{"url": value, "name": "原始附件_" + str(index) + ".pdf"}
                                  for index, value in enumerate(dict.fromkeys(attachments), 1)]
        article["metadata_raw"] = raw
        article["main_category"] = next((name for name, value in config_manager.categories_config.items()
                                          if re.fullmatch(r"/info/" + value["node_id"] + r"/\d+\.htm", urlsplit(url).path)), None)
        return article

    @staticmethod
    def Collect_dea(url, category, list_title=None, list_date=None, article_data=None,
                    start_date=None, end_date=None, source_relations=None):
        """原文件全部成功后才入库；逐项返回日期、资源或入库失败原因。"""
        stage = "detail"
        try:
            config = config_manager.get_category_config(category)
            if not config:
                raise ValueError("未配置的栏目：" + category)
            article = Cl._request_article(url, config)
            stage = "date"
            published = _publication_date(article.get(config["date_field"]))
            first = _publication_date(start_date) if start_date else None
            last = _publication_date(end_date) if end_date else None
            if (first and published < first) or (last and published > last):
                return {"success": True, "outside_range": True}
            date_warnings = []
            if list_date:
                try:
                    if published != _publication_date(list_date):
                        date_warnings.append({"url": url, "category": category, "list_date": list_date,
                                              "publication_date": published.isoformat(), "reason": "列表与详情日期不一致，以详情原始发布时间为准"})
                except ValueError:
                    date_warnings.append({"url": url, "category": category, "list_date": list_date,
                                          "publication_date": published.isoformat(), "reason": "列表日期不可解析，使用详情原始发布时间"})
            else:
                date_warnings.append({"url": url, "category": category, "list_date": list_date,
                                      "publication_date": published.isoformat(), "reason": "列表日期缺失，使用详情原始发布时间"})
            stage = "detail"
            title = str(article.get("title") or list_title or "").strip()
            main_category = article.get("main_category") or category
            main_config = config_manager.get_category_config(main_category)
            content_html = str(article.get("content_html") or "")
            html = etree.HTML(content_html) if content_html.strip() else None
            attachments = Cl._attachment_items(article.get("attachments"))
            video_items = Cl._attachment_items(article.get("news_video"))
            if html is not None:
                for node in html.xpath("//script|//style|//noscript"):
                    node.getparent().remove(node)
                text = "\n".join(part.strip() for part in html.xpath("//body//text()") if part.strip())
            else:
                text = ""
            if not text and not attachments and not video_items and not (html is not None and html.xpath("//img|//video")):
                raise ValueError("文章正文及原文件均为空，未标记采集成功")
            relations = source_relations or [{"栏目": category, "分类号": config["classification"], "网址": url,
                                              "列表网址": config["base_url"], "列表标题": list_title or title,
                                              "列表发布时间": list_date or ""}]
            resource_mappings = []
            if html is not None and urlsplit(url).hostname == "www.smzy.edu.cn":
                for image in html.xpath("//img"):
                    source = image.get("orisrc") or image.get("data-src") or image.get("src") or ""
                    original = urljoin(url, source.strip())
                    if original in config_manager.asset_url_overrides:
                        resource_mappings.append({"原始网址": original, "下载网址": Cl._asset_url(source, url)})
            article["resource_url_mappings"] = resource_mappings
            # 仅足够长的完全一致正文可自动合并，资源地址也参与指纹，避免合并同文异附件。
            normalized = re.sub(r"\s+", "", text)
            fingerprint = ""
            if len(normalized) >= 80:
                # 指纹与图片下载采用相同地址优先级，避免把不同懒加载原图误判为同一资源。
                resource_urls = [image.get("orisrc") or image.get("data-src") or image.get("src") or ""
                                 for image in (html.xpath("//img") if html is not None else [])]
                if html is not None:
                    resource_urls.extend(html.xpath("//video/@src|//video/source/@src|//a/@href"))
                resources = sorted({Cl._asset_url(value, url) for value in
                                    resource_urls
                                    if value.strip() and urlsplit(value).scheme in ("", "http", "https") and not value.startswith("#")}
                                   | {Cl._asset_url(item["url"] if isinstance(item, dict) else item, url)
                                      for item in attachments + video_items})
                fingerprint = hashlib.sha256(json.dumps([config["base_domain"], re.sub(r"\s+", "", title), published.isoformat(), normalized, resources],
                                                         ensure_ascii=False).encode("utf-8")).hexdigest()
            if fingerprint:
                existing = SqlClient.Cs.find_existing_article(url, fingerprint=fingerprint)
                if existing:
                    SqlClient.Cs.merge_source_relations(existing["id"], relations)
                    return {"success": True, "duplicate": True, "record_id": existing["id"],
                            "url": existing["url"], "date_warnings": date_warnings}
            article_dir = (Path(os.environ.get("NEWS_DATA_DIR", "/var/lib/news/data"))
                           / os.environ.get("NEWS_SITE_ID", "smzy") / main_category / str(published.year)
                           / hashlib.sha256(url.encode("utf-8")).hexdigest())
            article_dir.mkdir(parents=True, exist_ok=True)
            Cl._write_file(article_dir / "source.html", article["source_html"].encode("utf-8"))
            Cl._write_file(article_dir / "content.html", content_html.encode("utf-8"))
            Cl._write_file(article_dir / "content.txt", text.encode("utf-8"))
            stage = "file"
            images = Cl._save_images(html, article_dir, title, category, url) if html is not None else []
            videos = Cl._save_videos(html, article_dir, title, category, url) if html is not None else []
            videos.extend(Cl._save_attachments(video_items, None, article_dir, url, expect_type="video"))
            documents = Cl._save_attachments(attachments, html, article_dir, url)
            files = {"page_pdf": 0, "images": len(images), "videos": len(videos),
                     "original_pdf": sum(Path(next(iter(item.values()))).suffix.lower() == ".pdf" for item in documents),
                     "other_attachments": sum(Path(next(iter(item.values()))).suffix.lower() != ".pdf" for item in documents)}
            if main_category == "在线视频" and not videos:
                raise ValueError("在线视频正文没有可保存的站内视频原件")
            pdf_enabled = os.environ.get("NEWS_PDF_ENABLED", "true").lower() not in ("false", "0", "no")
            if pdf_enabled:
                pdf_path = Cl._generate_pdf(url, article_dir, title)
                if not pdf_path:
                    raise RuntimeError("网页 PDF 生成失败，文章未标记为采集成功")
                documents.insert(0, {"网页副本_" + title + ".pdf": pdf_path})
                files["page_pdf"] = 1
            payload = {
                "网站名称": main_config["site_name"], "网站域名": config["base_domain"],
                "网址": url, "所属栏目": main_category, "标题": title,
                "发布时间": published.isoformat(), "原始发布时间": article["publication_raw"], "年度": str(published.year),
                "分类号": main_config["classification"], "所属栏目编号": main_config["category_code"],
                "来源": str(article.get("source") or ""), "作者": str(article.get("author") or ""),
                "编辑": str(article.get("editor") or ""), "具体新闻内容": text,
                "摘要": text[:500], "摘要生成方式": "正文前五百字", "采集人": "档案馆",
                "采集时间": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
                "来源链接": "", "内容指纹": fingerprint, "栏目来源关系": relations,
                "来源网址列表": [url], "文件统计": files,
                "资源地址映射": resource_mappings,
            }
            Cl._write_file(article_dir / "article.json", json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            Cl._write_file(article_dir / "source.json", json.dumps(article, ensure_ascii=False, indent=2).encode("utf-8"))
            stage = "database"
            record_id = SqlClient.Cs.ins(title, url, payload, images, videos, documents, {})
            if not record_id:
                raise RuntimeError("新闻表入库失败")
            logger.info("文章已采集：%s %s %s", main_category, published, url)
            return {"success": True, "record_id": record_id, "url": url, "files": files,
                    "payload": payload, "date_warnings": date_warnings}
        except Exception as exc:
            logger.exception("文章详情采集失败：%s %s", category, url)
            if "发布日期" in str(exc) or "发布时间" in str(exc):
                stage = "date"
            # 下载异常带上资源网址，HTTP 任务会持久化本次失败，重跑不删除历史任务。
            return {"success": False, "errors": [{"stage": stage, "reason": str(exc),
                                                   "resource_url": getattr(exc, "resource_url", None)}]}

    @staticmethod
    def _asset_url(value, page_url):
        """规范资源地址，站内 HTTP 资源统一使用 HTTPS。"""
        original = urljoin(page_url, str(value).strip())
        # 保留原页面中的旧地址；只对官网文章使用经过逐项核验的精确公开映射。
        mapped = (config_manager.asset_url_overrides.get(original, original)
                  if urlsplit(page_url).hostname == "www.smzy.edu.cn" else original)
        parsed = urlsplit(mapped)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("不支持的资源地址")
        site_hosts = {urlsplit(config["base_domain"]).hostname
                      for config in config_manager.categories_config.values()}
        scheme = "https" if parsed.hostname in site_hosts else parsed.scheme
        return urlunsplit((scheme, parsed.netloc, re.sub(r"/{2,}", "/", parsed.path), parsed.query, ""))

    @staticmethod
    def _write_file(path, content):
        """以同目录临时文件原子替换，避免中断后留下半份制品。"""
        temporary = Path(str(path) + ".part")
        temporary.write_bytes(content)
        temporary.replace(path)

    @staticmethod
    def _download(value, save_dir, page_url, display_name=None, expect_image=False, expect_type=None):
        """保留资源原扩展名和显示名，校验响应后原子保存。"""
        resource_url = Cl._asset_url(value, page_url)
        path_name = Path(urlsplit(resource_url).path).name or "attachment"
        extension = Path(path_name).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", extension) or extension in (".jsp", ".php", ".asp", ".aspx"):
            extension = ".bin"
        response = None
        try:
            response = safe_get(resource_url, stream=True, headers={"Referer": page_url})
            content_type = response.headers.get("Content-Type", "").lower()
            if expect_image and not content_type.startswith("image/"):
                raise ValueError("图片下载返回的内容类型不正确")
            if not expect_image and ("text/html" in content_type or "application/json" in content_type):
                raise ValueError("附件下载返回了页面或错误数据")
            mime = content_type.split(";", 1)[0].strip()
            disposition = response.headers.get("Content-Disposition", "")
            filename = re.search(r"filename\*=UTF-8''([^;]+)|filename=[\"']?([^;\"']+)", disposition, re.I)
            server_name = unquote(next((value for value in filename.groups() if value), "")) if filename else ""
            if extension == ".bin":
                candidate = Path(server_name or display_name or "").suffix.lower()
                extension = candidate if re.fullmatch(r"\.[a-z0-9]{1,10}", candidate) else {
                    "application/pdf": ".pdf", "video/mp4": ".mp4", "video/webm": ".webm",
                    "application/msword": ".doc", "application/zip": ".zip",
                }.get(mime, ".bin")
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
            expected_size = response.headers.get("Content-Length")
            if expected_size and not response.headers.get("Content-Encoding") and size != int(expected_size):
                raise ValueError(f"下载长度不完整：应有 {expected_size} 字节，实际 {size} 字节")
            with temporary.open("rb") as source:
                signature = source.read(64)
            if extension == ".pdf" and not signature.startswith(b"%PDF-"):
                raise ValueError("原始 PDF 文件签名不正确")
            if expect_type == "video":
                if not (mime.startswith("video/") or mime in ("application/octet-stream", "binary/octet-stream")):
                    raise ValueError("视频原件下载返回的内容类型不正确")
                if extension == ".mp4" and b"ftyp" not in signature:
                    raise ValueError("MP4 视频原件签名不正确")
            temporary.replace(path)
            name = str(display_name or server_name or path_name).strip()
            name = re.sub(r'[\\/\x00-\x1f]', "_", name) or path_name
            if not Path(name).suffix:
                name += extension
            return {name: str(path)}
        except Exception as exc:
            exc.resource_url = resource_url
            raise
        finally:
            if response is not None:
                response.close()

    @staticmethod
    def _save_images(html, save_dir, title, category, page_url=None):
        """下载正文图片，任何图片失败均阻止文章入库。"""
        urls = [image.get("orisrc") or image.get("data-src") or image.get("src") or ""
                for image in html.xpath("//img")]
        return [Cl._download(value, save_dir, page_url or config_manager.get_base_domain(category),
                             title + "_" + str(index) + Path(urlsplit(value).path).suffix, True)
                for index, value in enumerate(dict.fromkeys(urls), 1)
                if value.strip() and "/system/resource/images/fileTypeImages/" not in value]

    @staticmethod
    def _save_videos(html, save_dir, title, category, page_url=None):
        """下载正文视频并保留与原爬虫一致的路径列表。"""
        urls = html.xpath("//video/@src|//video/source/@src")
        return [Cl._download(value, save_dir, page_url or config_manager.get_base_domain(category), expect_type="video")
                for value in dict.fromkeys(urls) if value.strip()]

    @staticmethod
    def _save_attachments(attachments, html, save_dir, page_url, expect_type=None):
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
                if (Path(urlsplit(value).path).suffix.lower() in {
                    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z"}
                        or (urlsplit(value).path.endswith("/download.jsp") and "wbfileid=" in urlsplit(value).query)):
                    urls.setdefault(Cl._asset_url(value, page_url), "".join(anchor.itertext()).strip() or None)
        result, hashes = [], set()
        for value, name in urls.items():
            item = Cl._download(value, save_dir, page_url, name, expect_type=expect_type)
            with Path(next(iter(item.values()))).open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            if digest not in hashes:
                hashes.add(digest)
                result.append(item)
        return result

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
