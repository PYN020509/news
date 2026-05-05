import pymysql

# 测试环境数据库配置（已注释）
conn = pymysql.connect(
    host='127.0.0.1',
    port=3306,
    user='root',
    passwd='123456',
    db='archive-5.0.1',
    use_unicode=True,
    charset="utf8"
)
cursor = conn.cursor()

# 线上环境数据库配置
# conn = pymysql.connect(
#     host='********',
#     port=3306,
#     user='root',
#     passwd='**********',
#     db='archive-5.0.1',
#     use_unicode=True,
#     charset="utf8"
# )
# cursor = conn.cursor()


class Cs:
    @staticmethod
    def sel(id_):
        # 使用参数化查询来避免 SQL 注入和拼接错误
        sql = 'SELECT id FROM news_spider WHERE id = %s'
        cursor.execute(sql, (id_,))
        conn.commit()
        result = cursor.fetchall()
        return result
    @staticmethod
    def ins(title, url, dict_, image_path_list, video_path_list, pdf_list, info, is_valid_link=None):
        """
        插入新闻数据到数据库

        :param title: 新闻标题
        :param url: 新闻URL
        :param dict_: 详细内容
        :param image_path_list: 图片路径列表
        :param video_path_list: 视频路径列表
        :param pdf_list: PDF路径
        :param info: 信息内容
        :param is_valid_link: 链接有效性（有效为None，无效写原因）
        """
        # 处理列表数据，转换为JSON字符串
        image_path_list = str(image_path_list).replace("'", '"')
        video_path_list = str(video_path_list).replace("'", '"')
        pdf_list = str(pdf_list).replace("'", '"')

        # 将 info 转换为字符串
        info = str(info).replace("'", '"')

        # 使用新的表结构字段（ID自动递增）
        sql = """INSERT INTO news_spider (
                    title, url, is_valid_link, record_id, release_time,
                    dict_, image_path_list, video_path_list, pdf_path, info, error_json
                 ) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s)"""

        # 根据链接有效性设置record_id
        record_id = -1 if is_valid_link is not None else None

        # 准备插入的值（不包含ID，让数据库自动生成）
        values = (title, url, is_valid_link, record_id, dict_, image_path_list, video_path_list, pdf_list, info, 0)

        # 打印调试信息
        print(f"准备插入的值: {values}")

        try:
            cursor.execute(sql, values)
            conn.commit()
            print("存入数据库成功")
            return sql
        except Exception as e:
            print(f"数据库插入错误: {e}, SQL: {sql}")
            conn.rollback()
            return None

    @staticmethod
    def check_url_exists(url):
        """检查URL是否已存在于数据库中"""
        try:
            sql = "SELECT COUNT(*) FROM news_spider WHERE url = %s"
            cursor.execute(sql, (url,))
            count = cursor.fetchone()[0]
            return count > 0
        except Exception as e:
            print(f"检查URL是否存在时出错: {e}")
            return False

    @staticmethod
    def get_news_by_url(url):
        """根据URL获取新闻记录"""
        try:
            sql = "SELECT * FROM news_spider WHERE url = %s"
            cursor.execute(sql, (url,))
            result = cursor.fetchone()
            return result
        except Exception as e:
            print(f"根据URL获取新闻记录时出错: {e}")
            return None

    @staticmethod
    def get_news_by_id(news_id):
        """根据ID获取新闻记录"""
        try:
            sql = "SELECT * FROM news_spider WHERE id = %s"
            cursor.execute(sql, (news_id,))
            result = cursor.fetchone()
            return result
        except Exception as e:
            print(f"根据ID获取新闻记录时出错: {e}")
            return None

    @staticmethod
    def update_news_by_id(news_id, **kwargs):
        """根据ID更新新闻记录"""
        try:
            # 构建更新字段
            update_fields = []
            values = []

            for field, value in kwargs.items():
                if field in ['title', 'url', 'is_valid_link', 'record_id', 'release_time',
                           'dict_', 'image_path_list', 'video_path_list', 'pdf_path', 'info', 'error_json']:
                    update_fields.append(f"{field} = %s")
                    values.append(value)

            if not update_fields:
                print("没有有效的更新字段")
                return False

            values.append(news_id)
            sql = f"UPDATE news_spider SET {', '.join(update_fields)} WHERE id = %s"

            cursor.execute(sql, values)
            conn.commit()
            print(f"更新新闻记录成功，ID: {news_id}")
            return True

        except Exception as e:
            print(f"更新新闻记录时出错: {e}")
            conn.rollback()
            return False

    @staticmethod
    def delete_news_by_id(news_id):
        """根据ID删除新闻记录"""
        try:
            sql = "DELETE FROM news_spider WHERE id = %s"
            cursor.execute(sql, (news_id,))
            conn.commit()
            print(f"删除新闻记录成功，ID: {news_id}")
            return True
        except Exception as e:
            print(f"删除新闻记录时出错: {e}")
            conn.rollback()
            return False

    @staticmethod
    def get_news_count():
        """获取新闻总数"""
        try:
            sql = "SELECT COUNT(*) FROM news_spider"
            cursor.execute(sql)
            count = cursor.fetchone()[0]
            return count
        except Exception as e:
            print(f"获取新闻总数时出错: {e}")
            return 0

    @staticmethod
    def get_valid_news_count():
        """获取有效新闻总数"""
        try:
            sql = "SELECT COUNT(*) FROM news_spider WHERE is_valid_link IS NULL"
            cursor.execute(sql)
            count = cursor.fetchone()[0]
            return count
        except Exception as e:
            print(f"获取有效新闻总数时出错: {e}")
            return 0

    @staticmethod
    def get_invalid_news_count():
        """获取无效新闻总数"""
        try:
            sql = "SELECT COUNT(*) FROM news_spider WHERE is_valid_link IS NOT NULL"
            cursor.execute(sql)
            count = cursor.fetchone()[0]
            return count
        except Exception as e:
            print(f"获取无效新闻总数时出错: {e}")
            return 0
