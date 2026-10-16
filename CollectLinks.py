import logging
import os
import re
import time
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ConfigManager import config_manager


logger = logging.getLogger(__name__)


class NewsCollector:
    """遍历真实列表接口，按发布时间采集三个归档栏目。"""

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
            "User-Agent": "GSZY-ArchiveCollector/1.0", "Accept": "application/json",
            "Referer": "https://www.gszy.edu.cn/reception/index",
        })
        self.base_urls = config_manager.get_active_categories()
        self.discovered_articles = []
        self._seen = set()
        self.summary = None
        self._stopped_reason = None

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
        self.summary = {
            "start_date": self.start_date.isoformat() if self.start_date is not None else None,
            "end_date": self.end_date.isoformat() if self.end_date is not None else None,
            "dry_run": self.dry_run, "success": False, "matched": 0, "collected": 0,
            "existing": 0, "failed": 0, "page_failures": 0, "categories": {}, "errors": [],
        }
        for category in self.base_urls:
            stats = {
                "pages_expected": 0, "pages_processed": 0, "total_reported": 0, "listed": 0,
                "matched": 0, "collected": 0, "existing": 0, "duplicates": 0,
                "outside_range": 0, "skipped": 0, "failed": 0, "page_failures": 0,
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
        for name in ("matched", "collected", "existing", "failed", "page_failures"):
            self.summary[name] = sum(stats[name] for stats in self.summary["categories"].values())
        self.summary["success"] = all(stats["complete"] for stats in self.summary["categories"].values())
        return self.summary

    def _post_json(self, url, data):
        """使用校验证书的 HTTP 请求获取接口数据。"""
        self._check_running()
        if self.request_interval:
            if self.stop_event is not None:
                if self.stop_event.wait(self.request_interval):
                    raise InterruptedError("服务已接到停止信号，采集任务保持未完成")
            else:
                time.sleep(self.request_interval)
        response = self.session.post(url, data=data, timeout=(10, 45))
        response.raise_for_status()
        return response.json()

    def _request_page(self, config, page):
        payload = self._post_json(config["list_api"], {
            config["type_param"]: config["type_id"], "pageNo": page, "title": "",
        })
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise ValueError(f"列表接口返回错误：{payload!r}")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ValueError("列表接口缺少分页数据或文章列表")
        for key in ("totalRow", "totalPage", "pageNumber", "pageSize"):
            value = data.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"列表接口分页字段异常：{key}={value!r}")
        if data["pageNumber"] != page:
            raise ValueError(f"请求第 {page} 页却返回第 {data['pageNumber']} 页")
        if data["totalRow"] and (not data["pageSize"] or not data["totalPage"]):
            raise ValueError("列表接口总数与分页信息不一致")
        if data["pageSize"]:
            expected_pages = (data["totalRow"] + data["pageSize"] - 1) // data["pageSize"]
            if data["totalPage"] != expected_pages:
                raise ValueError("列表接口总页数与总条数不一致")
        return data

    def _collect_category(self, category, stats):
        config = config_manager.get_category_config(category)
        first_page = self._request_page(config, 1)
        stats["pages_expected"] = max(first_page["totalPage"], 1)
        stats["total_reported"] = first_page["totalRow"]
        logger.info("开始采集 %s，共 %s 页、%s 条，日期 %s 至 %s",
                    category, stats["pages_expected"], stats["total_reported"],
                    self.start_date, self.end_date)
        consecutive_failures = 0
        for page in range(1, stats["pages_expected"] + 1):
            try:
                self._check_running()
                data = first_page if page == 1 else self._request_page(config, page)
                if data["totalRow"] != stats["total_reported"] or max(data["totalPage"], 1) != stats["pages_expected"]:
                    raise ValueError("采集期间列表总数发生变化，请重新运行以保证完整性")
                expected_count = min(data["pageSize"], max(data["totalRow"] - (page - 1) * data["pageSize"], 0))
                if len(data["list"]) != expected_count:
                    raise ValueError(f"第 {page} 页应有 {expected_count} 条，实际 {len(data['list'])} 条")
                stats["pages_processed"] += 1
                stats["listed"] += len(data["list"])
                consecutive_failures = 0
                # 置顶和编辑后的历史文章可能乱序，必须遍历所有分页。
                for item in data["list"]:
                    self._process_news_item(item, category, config, stats)
                if page == 1 or page % 20 == 0 or page == stats["pages_expected"]:
                    logger.info("列表进度：%s %s/%s 页，范围内 %s 条", category, page,
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

    def _process_news_item(self, item, category, config, stats):
        url = ""
        try:
            self._check_running()
            if not isinstance(item, dict):
                raise ValueError("文章列表项必须是对象")
            article_id = str(item.get("id") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", article_id):
                raise ValueError(f"文章编号异常：{article_id!r}")
            url = self._resolve_link(config["article_url_template"].format(id=article_id), category)
            list_date = self._parse_date(item.get(config["date_field"]) or "")
            key = (category, url)
            if key in self._seen:
                stats["duplicates"] += 1
                # 重复的分页项可能意味着接口翻页偏移，不能据此确认已完整采集。
                raise ValueError("同一栏目分页出现重复文章，请重新核对列表完整性")
            self._seen.add(key)
            if ((self.start_date is not None and list_date < self.start_date)
                    or (self.end_date is not None and list_date > self.end_date)):
                stats["outside_range"] += 1
                return
            stats["matched"] += 1
            self.discovered_articles.append({
                "category": category, "classification": config["classification"],
                "url": url, "date": list_date.isoformat(), "title": item.get("title") or "",
                "article_data": item,
            })
            if self.dry_run:
                return
            from SqlClient import Cs
            if Cs.check_url_exists(url, category=category):
                stats["existing"] += 1
                return
            self._check_running()
            from CollectDetail import Cl
            result = Cl.Collect_dea(
                url, category, list_title=item.get("title"),
                list_date=list_date.isoformat(), article_data=item,
                start_date=self.start_date.isoformat() if self.start_date is not None else None,
                end_date=self.end_date.isoformat() if self.end_date is not None else None,
            )
            if not isinstance(result, dict) or result.get("success") is False:
                raise ValueError("详情或附件采集、数据库入库未成功")
            if result.get("skipped"):
                stats["skipped"] += 1
            else:
                stats["collected"] += 1
        except InterruptedError:
            raise
        except Exception as exc:
            self._failure(category, str(exc), url=url)
            logger.error("文章采集失败：%s %s：%s", category, url, exc)

    def _check_running(self):
        """在分页和文章边界响应停止信号，不将未完任务标记为成功。"""
        if self.stop_event is not None and self.stop_event.is_set():
            raise InterruptedError("服务已接到停止信号，采集任务保持未完成")

    def _failure(self, category, reason, page=None, url=None, page_failure=False):
        """汇总失败原因，供补采和任务完成判断使用。"""
        stats = self.summary["categories"][category]
        stats["page_failures" if page_failure else "failed"] += 1
        self.summary["errors"].append({
            "category": category, "page": page, "url": url, "reason": reason,
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
        patterns = config_manager.get_valid_url_patterns(category)
        if patterns and not any(canonical.startswith(pattern) for pattern in patterns):
            raise ValueError(f"文章地址不属于允许的栏目路径：{canonical}")
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
