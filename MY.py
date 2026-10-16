from ConfigManager import load_image_configuration


load_image_configuration()


import argparse
import hashlib
import json
import logging
import os
import signal
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def state_identity():
    """把任务报告绑定到数据库和栏目配置，避免更换目标后混用报告。"""
    from ConfigManager import config_manager
    identity = {
        "database_host": os.environ.get("NEWS_DB_HOST"),
        "database_port": os.environ.get("NEWS_DB_PORT", "3306"),
        "database_name": os.environ.get("NEWS_DB_NAME"),
        "site": os.environ.get("NEWS_SITE_ID", "gszy"),
        "categories": config_manager.categories_config,
    }
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


@contextmanager
def task_lock(directory):
    """阻止服务和手动补采同时写入同一份状态及文件。"""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    lock_path = path / "collector.lock"
    lock_file = lock_path.open("a+b")
    locked = False
    try:
        lock_file.seek(0)
        if lock_path.stat().st_size == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError("同一目录已有采集进程，请先停止已有服务") from exc
        yield
    finally:
        if locked:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


@contextmanager
def collection_locks(directory):
    """同时锁定状态目录和资料目录，防止多实例在共享资料目录并发采集。"""
    paths = {Path(directory).resolve(), Path(os.environ.get("NEWS_DATA_DIR", "/var/lib/news/data")).resolve()}
    with ExitStack() as stack:
        for path in sorted(paths, key=str):
            stack.enter_context(task_lock(path))
        yield


def collect(mode, year=None, dry_run=False, stop_event=None):
    """复用既有采集器执行全量或固定年度任务，不在服务中决定执行时间。"""
    if mode not in ("all", "previous_year"):
        raise ValueError("采集参数只允许 all 或 previous_year")
    if mode == "previous_year":
        year = year if year is not None else datetime.now(SHANGHAI).year - 1
        if not isinstance(year, int) or isinstance(year, bool) or not 1900 <= year < datetime.now(SHANGHAI).year:
            raise ValueError("采集年度必须是已经结束的一九〇〇年及以后的自然年")
    elif year is not None:
        raise ValueError("全量采集不得指定年度")
    collector = None
    try:
        if not dry_run:
            from SqlClient import Cs
            Cs.check_schema()
        from CollectLinks import NewsCollector
        collector = NewsCollector(start_date=f"{year}-01-01" if year is not None else None,
                                  end_date=f"{year}-12-31" if year is not None else None,
                                  dry_run=dry_run, stop_event=stop_event)
        return collector.collect_news()
    except Exception as exc:
        logger.exception("采集失败：%s %s", mode, year)
        return {"success": False, "errors": [{"reason": str(exc)}]}
    finally:
        if collector is not None:
            collector.close()


def collect_year(year, dry_run=False, stop_event=None):
    """保留手动年度补采入口，复用与 HTTP 服务相同的采集逻辑。"""
    return collect("previous_year", year=year, dry_run=dry_run, stop_event=stop_event)


def main(argv=None):
    parser = argparse.ArgumentParser(description="甘肃中医药大学新闻网归档采集服务")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="采集后退出，未指定年度时默认采集上一年")
    mode.add_argument("--dry-run", action="store_true", help="只读遍历列表统计，不下载正文、不入库")
    mode.add_argument("--check-db", action="store_true", help="只读检查现有新闻采集表结构")
    mode.add_argument("--init-archive", action="store_true", help="通过电子档案业务接口建立归档库、标签及栏目映射")
    parser.add_argument("--year", type=int, help="手动补采或只读统计的自然年")
    parser.add_argument("--mode", choices=("all", "previous_year"),
                        help="手动采集范围：全部历史或上一自然年")
    args = parser.parse_args(argv)
    if args.year is not None and not (args.once or args.dry_run):
        parser.error("--year 必须与 --once 或 --dry-run 一起使用")
    if args.mode is not None and not (args.once or args.dry_run):
        parser.error("--mode 必须与 --once 或 --dry-run 一起使用")
    if args.mode == "all" and args.year is not None:
        parser.error("--mode all 不允许指定 --year")
    if args.year is not None and not 1900 <= args.year < datetime.now(SHANGHAI).year:
        parser.error("手动年度必须是已经结束的一九〇〇年及以后的自然年")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.check_db:
            from SqlClient import Cs
            Cs.check_schema()
            logger.info("新闻采集表结构检查通过")
            return 0
        if args.init_archive:
            from ArchiveInitializer import initialize_archive
            state_dir = os.environ.get("NEWS_STATE_DIR", "/var/lib/news/state")
            with task_lock(state_dir):
                result = initialize_archive()
            logger.info("归档库初始化完成：%s", json.dumps(result, ensure_ascii=False))
            return 0
        directory = os.environ.get("NEWS_STATE_DIR", "/var/lib/news/state")
        if args.dry_run:
            report = collect(args.mode or "previous_year", year=args.year, dry_run=True)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("success") else 1
        with collection_locks(directory):
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            if args.once:
                report = collect(args.mode or "previous_year", year=args.year, stop_event=stop)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 0 if report.get("success") else 1
            from NewsApp import serve_app
            return serve_app(directory, stop, collect, state_identity())
    except Exception:
        logger.exception("采集服务未完成")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
