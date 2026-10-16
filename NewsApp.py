import hmac
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")
ACTIVE_STATUSES = {"queued", "running"}
TASK_STATUSES = ACTIVE_STATUSES | {"succeeded", "failed", "interrupted"}


def collection_options(mode, now=None):
    """提交时固定采集范围，全量不设置年度过滤。"""
    if mode not in ("all", "previous_year"):
        raise ValueError("mode 只允许 all 或 previous_year")
    current = now if now is not None else datetime.now(SHANGHAI)
    if current.tzinfo is None:
        raise ValueError("计算上一年时必须提供包含时区的时间")
    year = current.astimezone(SHANGHAI).year - 1 if mode == "previous_year" else None
    return {
        "mode": mode, "year": year,
        "start_date": f"{year}-01-01" if year is not None else None,
        "end_date": f"{year}-12-31" if year is not None else None,
    }


def timestamp():
    """统一使用上海时间记录任务状态。"""
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


class CollectionTasks:
    """串行执行 HTTP 采集任务并原子保存报告，服务本身不安排采集时间。"""

    def __init__(self, directory, stop_event, collector, identity):
        self.path = Path(directory) / "api-tasks.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stop_event = stop_event
        self.collector = collector
        self.identity = identity
        self.mutex = threading.RLock()
        self.active_id = None
        self.worker = None
        self.storage_error = False
        self.tasks = {}
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or state.get("schema_version") != 1
                    or state.get("identity") != identity or not isinstance(state.get("tasks"), dict)):
                raise RuntimeError("HTTP 任务状态与当前数据库或栏目不一致，请备份后使用新的状态目录")
            self.tasks = state["tasks"]
            for task_id, task in self.tasks.items():
                if (not re.fullmatch(r"[0-9a-f]{32}", task_id)
                        or not isinstance(task, dict) or task.get("task_id") != task_id
                        or task.get("status") not in TASK_STATUSES):
                    raise RuntimeError("HTTP 任务状态文件损坏，已停止启动以保留原报告")
                if task["status"] in ACTIVE_STATUSES:
                    task.update(status="interrupted", ended_at=timestamp(),
                                message="服务停止或重启，任务未完成；重新调用即可跳过已成功采集的文章",
                                result={"success": False, "errors": [{"reason": "服务停止或重启"}]})
            self._persist()

    def _persist(self):
        """只在互斥锁保护下写入，用替换保证断电或中断后保留完整状态。"""
        temporary = self.path.with_suffix(".json.part")
        value = {"schema_version": 1, "identity": self.identity, "tasks": self.tasks}
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(self.path)

    @staticmethod
    def _snapshot(task):
        """返回独立快照，防止 HTTP 序列化时工作线程修改任务。"""
        return json.loads(json.dumps(task, ensure_ascii=False))

    def submit(self, mode):
        """已有任务执行时拒绝重复提交，已结束任务可再次调用并依数据库去重。"""
        options = collection_options(mode)
        with self.mutex:
            if self.stop_event.is_set() or self.storage_error:
                return HTTPStatus.SERVICE_UNAVAILABLE, {
                    "success": False, "message": "服务正在停止或任务状态不可写，请检查服务日志",
                }
            if self.active_id is not None:
                active = self._snapshot(self.tasks[self.active_id])
                return HTTPStatus.CONFLICT, {**active, "success": False, "message": "已有采集任务正在执行"}
            task_id = uuid.uuid4().hex
            task = {
                "task_id": task_id, "status": "queued", **options,
                "status_url": "/tasks/" + task_id, "created_at": timestamp(),
                "started_at": None, "ended_at": None, "message": "等待采集", "result": None,
            }
            self.tasks[task_id] = task
            try:
                self._persist()
            except Exception:
                self.storage_error = True
                self.tasks.pop(task_id)
                logger.exception("保存待执行采集任务失败")
                return HTTPStatus.SERVICE_UNAVAILABLE, {"success": False, "message": "任务状态无法保存，未开始采集"}
            self.active_id = task_id
            self.worker = threading.Thread(target=self._run, args=(task_id,), name="news-collection", daemon=True)
            response = {"success": True, **self._snapshot(task)}
            try:
                self.worker.start()
            except Exception:
                self.active_id = None
                task.update(status="failed", ended_at=timestamp(), message="采集线程无法启动",
                            result={"success": False, "errors": [{"reason": "采集线程无法启动"}]})
                try:
                    self._persist()
                except Exception:
                    self.storage_error = True
                    logger.exception("保存采集线程启动失败状态失败")
                logger.exception("采集线程无法启动")
                return HTTPStatus.SERVICE_UNAVAILABLE, {"success": False, **self._snapshot(task)}
            return HTTPStatus.ACCEPTED, response

    def _run(self, task_id):
        """任务只有完整成功才标记 succeeded，部分失败和停止都有明确结果。"""
        result = None
        try:
            with self.mutex:
                task = self.tasks[task_id]
                task.update(status="running", started_at=timestamp(), message="正在采集")
                self._persist()
                mode, year = task["mode"], task["year"]
            result = self.collector(mode, year=year, stop_event=self.stop_event)
            if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
                raise RuntimeError("采集器未返回有效统计结果")
            if self.stop_event.is_set():
                status, message = "interrupted", "服务已停止，任务未完成；重新调用可继续采集"
            elif result["success"]:
                status, message = "succeeded", "采集完成，已采集文章已跳过"
            else:
                status, message = "failed", "采集存在失败，请查看 result.errors 并重新调用"
        except BaseException as exc:
            logger.exception("HTTP 采集任务失败：%s", task_id)
            status = "interrupted" if self.stop_event.is_set() else "failed"
            message = "采集未完成：" + str(exc)
            result = {"success": False, "errors": [{"reason": str(exc)}]}
        if status == "interrupted":
            result["success"] = False
        with self.mutex:
            task = self.tasks[task_id]
            task.update(status=status, ended_at=timestamp(), message=message, result=result)
            try:
                self._persist()
            except Exception:
                self.storage_error = True
                task.update(status="failed", message="任务结果无法保存，请检查状态目录及服务日志")
                task["result"]["success"] = False
                logger.exception("保存采集完成状态失败：%s", task_id)
            self.active_id = None
        logger.info("采集任务结果：%s %s", task_id, json.dumps(task, ensure_ascii=False))

    def get(self, task_id):
        """查询持久化任务，未知编号明确返回不存在。"""
        with self.mutex:
            task = self.tasks.get(task_id)
            return self._snapshot(task) if task is not None else None

    def health(self):
        """仅检查服务和任务状态存储可用性，数据库在实际采集前核验。"""
        with self.mutex:
            ready = not self.stop_event.is_set() and not self.storage_error
            return ready, {"success": ready, "status": "ready" if ready else "unavailable",
                           "active_task_id": self.active_id}

    def close(self):
        """通知采集器停止，让当前文章有机会完成事务，再结束服务。"""
        self.stop_event.set()
        if self.worker is not None:
            self.worker.join(timeout=30)


def request_handler(tasks, token=""):
    """构造轻量 HTTP 接口，不引入额外框架和依赖。"""
    class NewsRequestHandler(BaseHTTPRequestHandler):
        def _respond(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                logger.info("HTTP 调用方已断开，持久化采集任务继续执行")

        def _authorized(self):
            if not token or hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"),
                                                 ("Bearer " + token).encode("utf-8")):
                return True
            self._respond(HTTPStatus.UNAUTHORIZED, {"success": False, "message": "采集服务令牌不正确"})
            return False

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                ready, result = tasks.health()
                self._respond(HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE, result)
                return
            if re.fullmatch(r"/tasks/[0-9a-f]{32}", path):
                if not self._authorized():
                    return
                task = tasks.get(path.rsplit("/", 1)[-1])
                self._respond(HTTPStatus.OK if task is not None else HTTPStatus.NOT_FOUND,
                              {"success": True, **task} if task is not None else
                              {"success": False, "message": "采集任务不存在"})
                return
            self._respond(HTTPStatus.NOT_FOUND, {"success": False, "message": "接口不存在"})

        def do_POST(self):
            if urlsplit(self.path).path != "/collect":
                self._respond(HTTPStatus.NOT_FOUND, {"success": False, "message": "接口不存在"})
                return
            if not self._authorized():
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("请使用带 Content-Length 的请求体")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("请求体不能为空且不得超过四千零九十六字节")
                self.connection.settimeout(15)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("请求体长度不完整")
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type == "application/json":
                    payload = json.loads(body.decode("utf-8"))
                elif content_type == "application/x-www-form-urlencoded":
                    values = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
                    if any(len(value) != 1 for value in values.values()):
                        raise ValueError("请求参数不允许重复")
                    payload = {key: value[0] for key, value in values.items()}
                else:
                    self._respond(HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                                  {"success": False, "message": "请使用 JSON 或表单请求体"})
                    return
                if not isinstance(payload, dict) or set(payload) != {"mode"} or not isinstance(payload["mode"], str):
                    raise ValueError("请求体只允许包含字符串参数 mode")
                status, result = tasks.submit(payload["mode"])
                self._respond(status, result)
            except (ValueError, UnicodeError, TimeoutError) as exc:
                self._respond(HTTPStatus.BAD_REQUEST, {"success": False, "message": str(exc)})
            except Exception:
                logger.exception("处理采集 HTTP 请求失败")
                self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, {"success": False, "message": "服务处理失败，请查看日志"})

        def log_message(self, format_, *args):
            logger.info("HTTP %s %s", self.client_address[0], format_ % args)

    return NewsRequestHandler


def serve_app(directory, stop_event, collector, identity):
    """启动只供 ERMS 调用的服务；采集范围和时间都由调用方决定。"""
    host = os.environ.get("NEWS_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("NEWS_HTTP_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("NEWS_HTTP_PORT 必须为一至六万五千五百三十五")
    tasks = CollectionTasks(directory, stop_event, collector, identity)
    server = ThreadingHTTPServer((host, port), request_handler(tasks, os.environ.get("NEWS_API_TOKEN", "")))
    server.daemon_threads = True
    server.timeout = 0.5
    logger.info("新闻采集 HTTP 服务已监听 %s:%s，由 ERMS 定时任务调用 /collect", host, port)
    try:
        while not stop_event.is_set():
            server.handle_request()
    finally:
        server.server_close()
        tasks.close()
    return 0
