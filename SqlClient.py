import json
import os
from contextlib import contextmanager


@contextmanager
def database_connection():
    """按需连接现场数据库，导入模块时不连接，也不采用开发环境凭据。"""
    import pymysql

    required = ("NEWS_DB_HOST", "NEWS_DB_USER", "NEWS_DB_PASSWORD", "NEWS_DB_NAME")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("缺少数据库配置：" + "、".join(missing))
    connection = pymysql.connect(
        host=os.environ["NEWS_DB_HOST"],
        port=int(os.environ.get("NEWS_DB_PORT", "3306")),
        user=os.environ["NEWS_DB_USER"],
        password=os.environ["NEWS_DB_PASSWORD"],
        database=os.environ["NEWS_DB_NAME"],
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
        autocommit=False,
    )
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


class Cs:
    """沿用电子档案系统的新闻采集表及字段，不在爬虫中创建数据库。"""

    @staticmethod
    def _select(sql, values=(), one=False):
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, values)
                return cursor.fetchone() if one else cursor.fetchall()

    @staticmethod
    def check_schema():
        """只读检查表结构，缺失时要求先执行电子档案系统的正式迁移。"""
        rows = Cs._select("SHOW COLUMNS FROM news_spider")
        columns = {row[0] for row in rows}
        required = {"id", "title", "url", "is_valid_link", "record_id", "release_time",
                    "dict_", "image_path_list", "video_path_list", "pdf_path", "info", "error_json"}
        missing = required - columns
        if missing:
            raise RuntimeError("news_spider 缺少字段：" + "、".join(sorted(missing)))
        return True

    @staticmethod
    def check_archive_ready():
        """采集前核验当前启用栏目已注册，防止后台将未绑定记录标记为异常。"""
        from ConfigManager import config_manager

        Cs.check_schema()
        from ArchiveInitializer import ACCESS_TEMPLATE_NAME
        rows = Cs._select(
            "SELECT a.access_b_i,a.template_id,a.table_id,a.table_name,a.data_status,a.tag_id,"
            "p.template_name,l.table_type_code,l.relate_type,l.wj_table_name,"
            "g.table_id,g.tag_name,g.pid FROM t_biz_access a "
            "LEFT JOIN t_biz_access_template p ON p.id=a.template_id "
            "LEFT JOIN t_library_table l ON l.id=a.table_id "
            "LEFT JOIN t_library_table_tag g ON g.id=a.tag_id "
            "WHERE a.client_key=%s AND a.api_key=%s",
            ("spider_news", "spider_user"),
        )
        template_name = os.environ.get("ERMS_ACCESS_TEMPLATE_NAME", ACCESS_TEMPLATE_NAME)
        checked = set()
        for category in config_manager.get_active_categories():
            matches = [row for row in rows if row[0] == category]
            if len(matches) != 1:
                raise RuntimeError("栏目归档配置缺失或重复：" + category + "；请先运行 --init-archive")
            row = matches[0]
            if (row[6] != template_name or row[7:9] != ("ER", "WJM") or row[3] != row[9]
                    or row[10] != row[2] or row[11] != category or row[12] != -1 or not row[4]):
                raise RuntimeError("栏目归档库、标签或专用字段模板不匹配：" + category)
            key = (row[1], row[3])
            if key in checked:
                continue
            fields = Cs._select(
                "SELECT field_name,access_field_name,convert_type,convert_value "
                "FROM t_biz_access_field WHERE template_id=%s", (row[1],),
            )
            mapping = {}
            for field in fields:
                name = str(field[0]).upper()
                if name in mapping:
                    raise RuntimeError("专用模板存在重复字段：" + name)
                mapping[name] = field
            required = {"WZMC": "网站名称", "WZYM": "网站域名", "SSLM": "所属栏目",
                        "ZTM": "标题", "WZ": "网址", "ZRBJ": "编辑", "FBSJ": "发布时间",
                        "ZZ": "作者", "ZY": "摘要", "JTXWNR": "具体新闻内容", "CJSJ": "采集时间",
                        "LAIY": "来源", "ND": "年度", "SSLMBH": "所属栏目编号", "FLH": "分类号"}
            for name, source in required.items():
                if name not in mapping or mapping[name][1] != source or mapping[name][2]:
                    raise RuntimeError("专用模板缺少正确的直接字段映射：" + source + " → " + name)
            collector = mapping.get("CJR")
            if not collector or collector[1:] != ("采集人", "fixed", "档案馆"):
                raise RuntimeError("专用模板缺少采集人固定值映射")
            import re
            if not re.fullmatch(r"[A-Za-z0-9_]+", row[3] or ""):
                raise RuntimeError("归档动态表名称不合法")
            columns = {str(item[0]).upper() for item in Cs._select("SHOW COLUMNS FROM `" + row[3] + "`")}
            defined = {str(item[0]).upper() for item in Cs._select(
                "SELECT field_name FROM t_library_field WHERE table_name=%s", (row[3],),
            )}
            missing = (set(mapping) - columns) | (set(mapping) - defined)
            if missing:
                raise RuntimeError("归档动态表缺少映射字段：" + "、".join(sorted(missing)))
            checked.add(key)
        return True

    @staticmethod
    def sel(id_):
        return Cs._select("SELECT id FROM news_spider WHERE id = %s", (id_,))

    @staticmethod
    def ins(title, url, dict_, image_path_list, video_path_list, pdf_list, info,
            is_valid_link=None):
        """事务入库；既有有效记录不覆盖，失败记录可重试，保留归档回写编号。"""
        payload = json.loads(dict_) if isinstance(dict_, str) else dict_
        info_value = json.loads(info) if isinstance(info, str) else info
        published = payload.get("发布时间") or None
        values = (
            title, url, is_valid_link, -1 if is_valid_link is not None else None,
            published, json.dumps(payload, ensure_ascii=False),
            json.dumps(image_path_list, ensure_ascii=False),
            json.dumps(video_path_list, ensure_ascii=False),
            json.dumps(pdf_list, ensure_ascii=False),
            json.dumps(info_value, ensure_ascii=False), 0,
        )
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT id, is_valid_link FROM news_spider WHERE url = %s FOR UPDATE", (url,))
                existing = cursor.fetchone()
                if existing and existing[1] is None:
                    return existing[0]
                if existing:
                    cursor.execute(
                        "UPDATE news_spider SET title=%s, url=%s, is_valid_link=%s, record_id=%s, "
                        "release_time=%s, dict_=%s, image_path_list=%s, video_path_list=%s, "
                        "pdf_path=%s, info=%s, error_json=%s WHERE id=%s",
                        values + (existing[0],),
                    )
                    record_id = existing[0]
                else:
                    cursor.execute(
                        "INSERT INTO news_spider (title, url, is_valid_link, record_id, release_time, "
                        "dict_, image_path_list, video_path_list, pdf_path, info, error_json) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", values,
                    )
                    record_id = cursor.lastrowid
            connection.commit()
            return record_id

    @staticmethod
    def check_url_exists(url, category=None):
        row = Cs._select(
            "SELECT dict_ FROM news_spider WHERE url = %s AND is_valid_link IS NULL", (url,), one=True,
        )
        if not row:
            return False
        if category:
            payload = json.loads(row[0])
            if payload.get("所属栏目") != category:
                raise RuntimeError("同一新闻网址已归属其他栏目，需人工核对栏目归档关系")
        return True

    @staticmethod
    def get_news_by_url(url):
        return Cs._select("SELECT * FROM news_spider WHERE url = %s", (url,), one=True)

    @staticmethod
    def get_news_by_id(news_id):
        return Cs._select("SELECT * FROM news_spider WHERE id = %s", (news_id,), one=True)

    @staticmethod
    def update_news_by_id(news_id, **kwargs):
        allowed = {"title", "url", "dict_", "image_path_list", "video_path_list", "pdf_path", "info", "error_json"}
        fields = [(key, value) for key, value in kwargs.items() if key in allowed]
        if not fields:
            return False
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE news_spider SET " + ", ".join(key + " = %s" for key, _ in fields)
                               + " WHERE id = %s", tuple(value for _, value in fields) + (news_id,))
                changed = cursor.rowcount > 0
            connection.commit()
            return changed

    @staticmethod
    def delete_news_by_id(news_id):
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM news_spider WHERE id = %s", (news_id,))
                changed = cursor.rowcount > 0
            connection.commit()
            return changed

    @staticmethod
    def get_news_count():
        return Cs._select("SELECT COUNT(*) FROM news_spider", one=True)[0]

    @staticmethod
    def get_valid_news_count():
        return Cs._select("SELECT COUNT(*) FROM news_spider WHERE is_valid_link IS NULL", one=True)[0]

    @staticmethod
    def get_invalid_news_count():
        return Cs._select("SELECT COUNT(*) FROM news_spider WHERE is_valid_link IS NOT NULL", one=True)[0]
