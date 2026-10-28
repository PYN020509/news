import logging
import os
import re
import time
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from lxml import etree
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ConfigManager import config_manager


logger = logging.getLogger(__name__)


class NewsCollector:
    """遍历神木四个栏目的全部历史分页，按文章原始发布日期采集。"""

    def __init__(self, start_date=None, end_date=None, dry_run=False, stop_event=None):
        self.start_date = self._parse_date(start_date) if start_date is not None else None
        self.end_date = self._parse_date(end_date) if end_date is not None else None
        if self.start_date is not None and self.end_date is not None and self.start_date > self.end_date:
            raise ValueError("采集开始日期不得晚于结束日期")
        self.dry_run = dry_run
        self.stop_event = stop_event
        self.request_interval = float(os.getenv("NEWS_REQUEST_INTERVAL", os.getenv("CRAWL_REQUEST_INTERVAL", "1.0")))
        if self.request_interval < 0:
            raise ValueError("请求间隔不得为负数")
        self.session = requests.Session()
        self.session.verify = os.environ.get("NEWS_VERIFY_TLS", "true").lower() not in ("false", "0", "no")
        retry = Retry(total=3, backoff_factor=1,
                      status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET", "POST"])
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update({
            "User-Agent": "SMZY-ArchiveCollector/1.0", "Accept": "text/html,application/xhtml+xml",
            "Referer": "https://www.smzy.edu.cn/",
        })
        self.base_urls = config_manager.get_active_categories()
        self.discovered_articles = []
        self._seen = set()
        self.summary = None
        self._stopped_reason = None
        self._source_urls = set()
        self._successful_records = {}

    @staticmethod
    def _parse_date(value):
        """校验日期参数并允许直接传入日期对象。"""
        if isinstance(value, date):
            return date(value.year, value.month, value.day)
        try:
            return date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"日期缺失或格式不正确：{value!r}") from exc

    def collect_news(self):
        """返回可供调用方判断是否完成的统计，失败不会标记为成功。"""
        self.discovered_articles = []
        self._seen = set()
        self._stopped_reason = None
        self._source_urls = set()
        self._successful_records = {}
        self.summary = {
            "start_date": self.start_date.isoformat() if self.start_date is not None else None,
            "end_date": self.end_date.isoformat() if self.end_date is not None else None,
            "dry_run": self.dry_run, "success": False, "matched": 0, "collected": 0,
            "existing": 0, "skipped": 0, "failed": 0, "page_failures": 0,
            "categories": {}, "errors": [], "skipped_links": [], "duplicate_links": [],
            "date_warnings": [], "unique_source_urls": 0,
            "date_basis": "列表日期，仅供只读估算" if self.dry_run else "详情原始发布日期或已采集原始发布日期",
        }
        for category in self.base_urls:
            stats = {
                "pages_expected": 0, "pages_processed": 0, "total_reported": 0, "listed": 0,
                "matched": 0, "collected": 0, "existing": 0, "duplicates": 0,
                "outside_range": 0, "skipped": 0, "failed": 0, "page_failures": 0,
                "cross_category_duplicates": 0, "content_duplicates": 0,
                "file_failures": 0, "date_failures": 0,
                "files": {"page_pdf": 0, "original_pdf": 0, "images": 0, "videos": 0, "other_attachments": 0},
            }
            self.summary["categories"][category] = stats
            try:
                if self._stopped_reason:
                    raise InterruptedError(self._stopped_reason)
                self._check_running()
                self._collect_category(category, stats)
            except InterruptedError as exc:
                self._stopped_reason = str(exc)
                self._failure(category, self._stopped_reason, page_failure=True)
                logger.warning("栏目采集已停止：%s %s", category, exc)
            except Exception as exc:
                self._failure(category, str(exc), page_failure=True)
                logger.exception("栏目采集失败：%s", category)
            stats["complete"] = (
                stats["page_failures"] == 0 and stats["failed"] == 0
                and stats["pages_processed"] == stats["pages_expected"]
                and stats["listed"] == stats["total_reported"]
            )
            logger.info("栏目统计：%s %s", category, stats)
        for name in ("matched", "collected", "existing", "skipped", "failed", "page_failures",
                     "listed", "duplicates", "cross_category_duplicates", "content_duplicates",
                     "file_failures", "date_failures", "outside_range"):
            self.summary[name] = sum(stats[name] for stats in self.summary["categories"].values())
        self.summary["unique_source_urls"] = len(self._source_urls)
        self.summary["source_urls"] = sorted(self._source_urls)
        self.summary.setdefault("article_urls", [])
        self.summary["unique_articles"] = len(self.summary["article_urls"])
        self.summary["files"] = {name: sum(stats["files"][name] for stats in self.summary["categories"].values())
                                 for name in ("page_pdf", "original_pdf", "images", "videos", "other_attachments")}
        self.summary["success"] = all(stats["complete"] for stats in self.summary["categories"].values())
        return self.summary

    def _get_html(self, url):
        """按既有节流和停止机制读取 UTF-8 列表。"""
        self._check_running()
        if self.request_interval:
            if self.stop_event is not None:
                if self.stop_event.wait(self.request_interval):
                    raise InterruptedError("服务已接到停止信号，采集任务保持未完成")
            else:
                time.sleep(self.request_interval)
        response = self.session.get(url, timeout=(10, 45))
        response.raise_for_status()
        html = etree.HTML(response.content)
        if html is None:
            raise ValueError("列表未返回有效 HTML")
        if urlsplit(response.url).hostname != urlsplit(url).hostname:
            raise ValueError("列表被重定向至其他网站")
        return html

    def _request_page(self, config, page, total_pages=None):
        """按首页与倒序历史页规则读取，并核对实际页码。"""
        if page == 1:
            url = config["base_url"]
        else:
            if total_pages is None or not 1 < page <= total_pages:
                raise ValueError("历史分页缺少有效总页数")
            url = config["page_url_template"].format(index=total_pages - page + 1)
        return self._parse_page(self._get_html(url), config, url, page)

    @staticmethod
    def _parse_page(html, config, url, page):
        """从分页栏及新闻卡片提取数据，结构异常时保留失败统计。"""
        pagination = html.xpath(config["pagination_xpath"])
        if len(pagination) != 1:
            raise ValueError("列表缺少唯一分页栏")
        page_size = config["items_per_page"]
        current = "".join(pagination[0].xpath(".//span[@class='p_no_d']//text()")).strip()
        if current != str(page):
            raise ValueError(f"请求第 {page} 页却返回第 {current or '未知'} 页")
        page_labels = pagination[0].xpath(".//span[@class='p_no']/a/text()") + [current]
        if any(not value.strip().isdigit() for value in page_labels):
            raise ValueError("列表分页编号不是整数")
        pages = max(map(int, page_labels))
        if not 1 <= page <= pages:
            raise ValueError("列表分页编号超出范围")
        last_links = pagination[0].xpath(".//span[contains(concat(' ',normalize-space(@class),' '),' p_last ')]/a/@href")
        if page < pages and (len(last_links) != 1
                             or urljoin(url, last_links[0]) != config["page_url_template"].format(index=1)):
            raise ValueError("列表尾页链接与倒序分页规则不一致")
        next_links = pagination[0].xpath(".//span[contains(concat(' ',normalize-space(@class),' '),' p_next ')]/a/@href")
        if page < pages:
            expected_next = config["page_url_template"].format(index=pages - page)
            if len(next_links) != 1 or urljoin(url, next_links[0]) != expected_next:
                raise ValueError("列表下页链接与倒序分页规则不一致")
        elif next_links:
            raise ValueError("列表尾页仍存在下页链接")
        items = []
        for anchor in html.xpath(config["list_xpath"]):
            href = anchor.get("href") or ""
            title = (anchor.get("title") or "".join(anchor.xpath(".//span[@class='tit']//text()"))).strip()
            published = "".join(anchor.xpath(".//p[contains(concat(' ',normalize-space(@class),' '),' time ')]/span[2]/text()")).strip()
            # 单条信息异常交由文章处理记录 URL，不能因此丢失同页其他文章。
            items.append({"url": urljoin(url, href.strip()), "title": title,
                          config["date_field"]: published, "list_url": url})
        if (page < pages and len(items) != page_size) or not 0 < len(items) <= page_size:
            raise ValueError(f"列表第 {page} 页条数异常：{len(items)}")
        return {"totalRow": None, "totalPage": pages, "pageNumber": page,
                "pageSize": page_size, "list": items}

    def _collect_category(self, category, stats):
        config = config_manager.get_category_config(category)
        first_page = self._request_page(config, 1)
        stats["pages_expected"] = max(first_page["totalPage"], 1)
        # 当前网站没有总条数字段，先核验真实尾页，按满页数与尾页条数推算。
        last_page = (self._request_page(config, stats["pages_expected"], stats["pages_expected"])
                     if stats["pages_expected"] > 1 else first_page)
        if last_page["totalPage"] != stats["pages_expected"]:
            raise ValueError("首页与尾页显示的总页数不一致")
        stats["total_reported"] = (stats["pages_expected"] - 1) * config["items_per_page"] + len(last_page["list"])
        logger.info("开始采集 %s，共 %s 页、%s 条，日期 %s 至 %s",
                    category, stats["pages_expected"], stats["total_reported"],
                    self.start_date, self.end_date)
        consecutive_failures = 0
        for page in range(1, stats["pages_expected"] + 1):
            try:
                self._check_running()
                data = (first_page if page == 1 else last_page if page == stats["pages_expected"]
                        else self._request_page(config, page, stats["pages_expected"]))
                if max(data["totalPage"], 1) != stats["pages_expected"]:
                    raise ValueError("采集期间列表页数发生变化，请重新运行以保证完整性")
                expected_count = min(data["pageSize"], max(stats["total_reported"] - (page - 1) * data["pageSize"], 0))
                if len(data["list"]) != expected_count:
                    raise ValueError(f"第 {page} 页应有 {expected_count} 条，实际 {len(data['list'])} 条")
                stats["pages_processed"] += 1
                stats["listed"] += len(data["list"])
                consecutive_failures = 0
                # 置顶和编辑后的历史文章可能乱序，必须遍历所有分页。
                for item in data["list"]:
                    self._process_news_item(item, category, config, stats)
                if page == 1 or page % 20 == 0 or page == stats["pages_expected"]:
                    logger.info("列表进度：%s %s/%s 页，范围内站内新闻 %s 条", category, page,
                                stats["pages_expected"], stats["matched"])
            except InterruptedError:
                raise
            except Exception as exc:
                self._failure(category, str(exc), page=page, page_failure=True)
                logger.error("列表页采集失败：%s 第 %s 页：%s", category, page, exc)
                network_failure = (
                    isinstance(exc, requests.exceptions.RequestException)
                    and not isinstance(exc, requests.exceptions.JSONDecodeError)
                )
                consecutive_failures = consecutive_failures + 1 if network_failure else 0
                if consecutive_failures >= 3:
                    raise RuntimeError("连续三个列表页 HTTP 请求失败，已停止该栏目，请查看失败报告后重试") from exc
        if stats["listed"] != stats["total_reported"]:
            self._failure(category, f"列表总数不一致：读取 {stats['listed']} 条，接口报告 {stats['total_reported']} 条", page_failure=True)
        # 长任务结束时重新检查两端，不能将采集中发生分页位移的结果当作完整快照。
        current_first = self._request_page(config, 1)
        current_last = (self._request_page(config, stats["pages_expected"], stats["pages_expected"])
                        if stats["pages_expected"] > 1 else current_first)
        if current_first != first_page or current_last != last_page:
            self._failure(category, "采集期间首页或尾页发生变化，请重跑核对列表完整性", page_failure=True)

    def _process_news_item(self, item, category, config, stats):
        """保留来源关系，年度范围以详情或已入库文章的原始发布时间为准。"""
        url = ""
        try:
            self._check_running()
            if not isinstance(item, dict):
                raise ValueError("文章列表项必须是对象")
            url = str(item.get("url") or "").strip()
            if not url or not str(item.get("title") or "").strip():
                raise ValueError("文章列表项缺少链接或标题")
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("列表文章地址格式不正确")
            external = parsed.hostname != urlsplit(config["base_domain"]).hostname
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")) if external else self._resolve_link(url, category)
            key = (category, url)
            if key in self._seen:
                stats["duplicates"] += 1
                return
            self._seen.add(key)
            raw_date = item.get(config["date_field"]) or ""
            try:
                list_date = self._parse_date(raw_date)
            except ValueError:
                list_date = None
            if self.dry_run or external:
                if list_date is None:
                    stats["date_failures"] += 1
                    raise ValueError("列表发布日期缺失或不可解析，无法确定未采集详情的年度")
                if not self._in_range(list_date):
                    stats["outside_range"] += 1
                    return
            if external:
                stats["skipped"] += 1
                self._source_urls.add(url)
                self.summary["skipped_links"].append({
                    "category": category, "url": url, "date": list_date.isoformat(),
                    "title": item.get("title") or "", "reason": "栏目链接指向外部网站，未采集站外正文",
                })
                return
            relation = {"栏目": category, "分类号": config["classification"], "网址": url,
                        "列表网址": item.get("list_url") or config["base_url"],
                        "列表标题": item.get("title") or "", "列表发布时间": str(raw_date)}
            self.discovered_articles.append({
                "category": category, "classification": config["classification"],
                "url": url, "date": str(raw_date), "title": item.get("title") or "", "article_data": item,
            })
            if self.dry_run:
                if url in self._source_urls:
                    stats["cross_category_duplicates"] += 1
                self._source_urls.add(url)
                stats["matched"] += 1
                return
            from SqlClient import Cs
            existing = Cs.find_existing_article(url)
            if existing:
                try:
                    published = self._parse_date(existing["payload"].get("发布时间"))
                except ValueError:
                    stats["date_failures"] += 1
                    raise
                if not self._in_range(published):
                    stats["outside_range"] += 1
                    return
                Cs.merge_source_relations(existing["id"], [relation])
                self._source_urls.add(url)
                stats["matched"] += 1
                self._record_article(existing["id"], existing["url"])
                if existing["id"] in self._successful_records:
                    stats["cross_category_duplicates"] += 1
                    self._duplicate(category, url, existing, "来源 URL 重复")
                else:
                    stats["existing"] += 1
                    self._successful_records[existing["id"]] = category
                return
            self._check_running()
            from CollectDetail import Cl
            result = Cl.Collect_dea(
                url, category, list_title=item.get("title"), list_date=str(raw_date) or None,
                article_data=item, source_relations=[relation],
                start_date=self.start_date.isoformat() if self.start_date is not None else None,
                end_date=self.end_date.isoformat() if self.end_date is not None else None,
            )
            if not isinstance(result, dict):
                raise ValueError("详情采集器未返回有效结果")
            if result.get("success") is False:
                for error in result.get("errors") or [{"reason": "详情采集失败"}]:
                    stage = error.get("stage", "detail")
                    if stage == "file":
                        stats["file_failures"] += 1
                    elif stage == "date":
                        stats["date_failures"] += 1
                    self._failure(category, error.get("reason", "详情采集失败"), url=url,
                                  stage=stage, resource_url=error.get("resource_url"))
                return
            if result.get("outside_range"):
                stats["outside_range"] += 1
                return
            self._source_urls.add(url)
            stats["matched"] += 1
            self.summary["date_warnings"].extend(result.get("date_warnings", []))
            self._record_article(result["record_id"], result["url"])
            if result.get("duplicate"):
                stats["content_duplicates"] += 1
                if self._successful_records.get(result["record_id"], category) != category:
                    stats["cross_category_duplicates"] += 1
                self._duplicate(category, url, result, "标题、原始日期及正文指纹一致")
                if result["record_id"] not in self._successful_records:
                    stats["existing"] += 1
            else:
                stats["collected"] += 1
                for name, count in result.get("files", {}).items():
                    stats["files"][name] += count
            self._successful_records[result["record_id"]] = self._successful_records.get(result["record_id"], category)
        except InterruptedError:
            raise
        except Exception as exc:
            self._failure(category, str(exc), url=url)
            logger.error("文章采集失败：%s %s：%s", category, url, exc)

    def _in_range(self, published):
        """统一核对自然年边界，不依据列表顺序提前终止分页。"""
        return not ((self.start_date is not None and published < self.start_date)
                    or (self.end_date is not None and published > self.end_date))

    def _record_article(self, record_id, url):
        """归档范围使用暂存表中的主网址，来源别名另行保留。"""
        urls = self.summary.setdefault("article_urls", [])
        if url not in urls:
            urls.append(url)

    def _duplicate(self, category, url, existing, reason):
        """将跨栏目及转载去重证据独立保存，不混入采集失败。"""
        self.summary["duplicate_links"].append({
            "category": category, "url": url, "canonical_url": existing["url"],
            "record_id": existing.get("id", existing.get("record_id")), "reason": reason,
        })

    def _check_running(self):
        """在分页和文章边界响应停止信号，不将未完任务标记为成功。"""
        if self.stop_event is not None and self.stop_event.is_set():
            raise InterruptedError("服务已接到停止信号，采集任务保持未完成")

    def _failure(self, category, reason, page=None, url=None, page_failure=False, stage=None, resource_url=None):
        """汇总失败原因，供补采和任务完成判断使用。"""
        stats = self.summary["categories"][category]
        stats["page_failures" if page_failure else "failed"] += 1
        self.summary["errors"].append({
            "category": category, "page": page, "url": url, "reason": reason,
            "stage": stage or ("page" if page_failure else "detail"), "resource_url": resource_url,
        })

    def _resolve_link(self, link, category=None):
        """规范站内文章地址，校验来源并去掉片段。"""
        base = config_manager.get_base_domain(category)
        if not base:
            raise ValueError(f"未配置栏目网站：{category}")
        absolute = urlsplit(urljoin(base + "/", link.strip()))
        expected = urlsplit(base)
        if absolute.scheme not in ("http", "https") or absolute.hostname != expected.hostname:
            raise ValueError(f"文章地址不属于目标网站：{link}")
        if absolute.port not in (None, 80, 443) or absolute.username or absolute.password:
            raise ValueError(f"文章地址包含非标准端口或身份信息：{link}")
        path = re.sub(r"/{2,}", "/", absolute.path)
        canonical = urlunsplit(("https", expected.netloc, path, absolute.query, ""))
        config = config_manager.get_category_config(category)
        if not re.fullmatch(config["detail_path_pattern"], path):
            raise ValueError(f"文章地址不是列表允许的站内详情：{canonical}")
        return canonical

    def close(self):
        """释放 HTTP 连接池。"""
        self.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    collector = NewsCollector()
    try:
        result = collector.collect_news()
        raise SystemExit(0 if result["success"] else 1)
    finally:
        collector.close()
