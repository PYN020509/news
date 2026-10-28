"""通过 ERMS 既有业务接口初始化新闻网电子档案库。

作者：PYN
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from ConfigManager import config_manager


ACCESS_TEMPLATE_NAME = "smzy_神木职业技术学院网页采集"
ARCHIVE_DESCRIPTION = "神木职业技术学院学院要闻、通知公告、党建动态和在线视频归档"
ARCHIVE_FIELDS = (
    "DH", "QZH", "ND", "FLH", "JH", "ZTM", "SSLM", "FBSJ", "ZZ", "LAIY", "CJR", "CJSJ",
    "WZ", "ZY", "JTXWNR", "ZRBJ", "WZMC", "WZYM", "SSLMBH", "SPIDERID", "FTM", "BGQX",
    "WH", "ZRZ", "MJ", "GDDW", "YS", "LRSJ", "GK", "BGHMJ", "XCBGQX", "YJBM", "JSBM",
    "YJR", "JSR", "YJSJ", "YJSM", "GDRQ",
)
LIST_FIELDS = ("DH", "ND", "FLH", "ZTM", "SSLM", "FBSJ", "ZZ", "LAIY", "CJR", "CJSJ")
SOURCE_FIELDS = {
    "WZMC": "网站名称", "WZYM": "网站域名", "SSLM": "所属栏目", "ZTM": "标题", "WZ": "网址",
    "ZRBJ": "编辑", "FBSJ": "发布时间", "ZZ": "作者", "ZY": "摘要", "JTXWNR": "具体新闻内容",
    "CJSJ": "采集时间", "LAIY": "来源", "ND": "年度", "SSLMBH": "所属栏目编号", "FLH": "分类号",
    "CJR": "采集人",
    "QZH": "全宗号",
}


class ArchiveInitializationError(RuntimeError):
    """初始化配置、接口响应或现场档案结构不符合要求。"""


class ArchiveInitializer:
    """复用建库、引用模板、标签授权和业务接入配置接口。"""

    def __init__(self, environment=None, session=None):
        self.environment = os.environ if environment is None else environment
        self.base_url = self._required("ERMS_BASE_URL").rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username:
            raise ArchiveInitializationError("ERMS_BASE_URL 必须为不含凭据的 HTTP 服务地址")
        if parsed.query or parsed.fragment:
            raise ArchiveInitializationError("ERMS_BASE_URL 不允许包含查询参数或片段")
        self.parent_id = self._integer("ERMS_PARENT_TABLE_ID", minimum=-1)
        if self.parent_id == 0:
            raise ArchiveInitializationError("父库编号必须为 -1 或实际存在的正整数")
        self.template_table_id = self._integer("ERMS_TEMPLATE_TABLE_ID")
        # 显式指定时只接入该既有业务库，绝不按名称查找或另建替代库。
        self.library_table_id = (self._integer("ERMS_LIBRARY_TABLE_ID")
                                 if self.environment.get("ERMS_LIBRARY_TABLE_ID", "").strip() else None)
        self.fond_id = self._integer("ERMS_FOND_ID")
        self.data_status = self._required("ERMS_DATA_STATUS")
        self.library_name = self.environment.get("ERMS_LIBRARY_NAME", "新闻网文章采集")
        self.access_template_name = self.environment.get(
            "ERMS_ACCESS_TEMPLATE_NAME", ACCESS_TEMPLATE_NAME
        )
        state_directory = Path(self.environment.get("NEWS_STATE_DIR", "/var/lib/news/state"))
        self.state_file = Path(self.environment.get(
            "ERMS_ARCHIVE_STATE_FILE", str(state_directory / "archive-initialization.json")
        ))
        self.categories = {
            name: item["classification"]
            for name, item in config_manager.categories_config.items()
        }
        self.session = session or requests.Session()
        token = self._required("ERMS_TOKEN")
        if not token.lower().startswith("bearer "):
            token = "Bearer " + token
        self.session.headers.update({"Authorization": token, "Accept": "application/json"})

    def _required(self, name):
        value = self.environment.get(name, "").strip()
        if not value:
            raise ArchiveInitializationError(f"请在现场环境配置中填写 {name}")
        return value

    def _integer(self, name, minimum=1):
        try:
            value = int(self._required(name))
        except ValueError as exc:
            raise ArchiveInitializationError(f"{name} 必须为整数") from exc
        if value < minimum:
            raise ArchiveInitializationError(f"{name} 必须不小于 {minimum}")
        return value

    def _api(self, method, path, params=None, body=None):
        """接口写操作失败后不自动重试，重启时重新核对实际业务状态。"""
        try:
            response = self.session.request(
                method, self.base_url + path, params=params, json=body,
                timeout=(10, 90), allow_redirects=False,
            )
        except requests.RequestException:
            raise ArchiveInitializationError(
                f"ERMS 接口 {method} {path} 连接失败；请检查服务与现场网络"
            ) from None
        if response.status_code in (401, 403):
            raise ArchiveInitializationError("ERMS 登录凭据失效或当前账号没有初始化权限")
        if not 200 <= response.status_code < 300:
            raise ArchiveInitializationError(
                f"ERMS 接口 {method} {path} 返回 HTTP {response.status_code}"
            )
        try:
            result = response.json()
        except (ValueError, TypeError):
            raise ArchiveInitializationError(f"ERMS 接口 {path} 未返回 JSON") from None
        if not isinstance(result, dict) or result.get("status") != 0:
            # 不回显服务完整响应，避免其中夹带 token 或其他现场凭据。
            raise ArchiveInitializationError(
                f"ERMS 接口 {method} {path} 业务处理失败；请查 ERMS 服务日志"
            )
        return result.get("data")

    @staticmethod
    def _list(value, label):
        if not isinstance(value, list):
            raise ArchiveInitializationError(f"{label} 的接口返回结构不符合预期")
        return value

    @staticmethod
    def _one(rows, label):
        if len(rows) != 1:
            raise ArchiveInitializationError(f"{label} 数量为 {len(rows)}，请排查缺失或重复配置")
        return rows[0]

    def _table(self, table_id):
        table = self._api("GET", f"/library/table/{table_id}")
        if not isinstance(table, dict) or table.get("id") != table_id:
            raise ArchiveInitializationError(f"库编号 {table_id} 不存在或无权访问")
        fond_id = table.get("fondId")
        if isinstance(fond_id, str):
            # 现场 Long 字段序列化为十进制字符串，仅规范类型，不改动原响应对象。
            if not fond_id.isascii() or not fond_id.isdecimal():
                raise ArchiveInitializationError("库所属全宗编号不是有效十进制整数")
            table = {**table, "fondId": int(fond_id)}
        return table

    def _fields(self, table_name):
        path = "/library/table/field/list/" + quote(table_name, safe="")
        # 完整读取模板字段，隐藏编辑字段也参与归档及结构兼容检查。
        result = self._api("GET", path, params={"editButton": "true"})
        if not isinstance(result, dict):
            raise ArchiveInitializationError("库字段定义接口返回结构不符合预期")
        rows = self._list(result.get("libraryFieldVOS"), "库字段定义")
        names = [str(row.get("fieldName", "")).upper() for row in rows]
        if len(set(names)) != len(names):
            raise ArchiveInitializationError("库字段定义中存在重复字段名称")
        return {name: row for name, row in zip(names, rows)}

    def _backup(self, label, value):
        """每次业务配置写入前保存现场完整对象，备份文件不包含登录凭据。"""
        directory = self.state_file.parent / "archive-backups"
        directory.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + label + ".json"
        target = directory / name
        with target.open("x", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
        target.chmod(0o600)

    def _mapping(self, source_fields):
        """通过既有业务接口补齐本站专用映射，已有不同配置须先人工核对。"""
        qzh = source_fields.get("QZH") or {}
        qzh_value = qzh.get("initValue")
        if qzh.get("initType") != "fixed" or not isinstance(qzh_value, str) or not qzh_value.strip():
            raise ArchiveInitializationError("网页归档库全宗号必须配置非空固定值，不能依赖采集归档流程自动套用字段默认值")
        templates = self._list(self._api(
            "GET", "/admin/bizAccessTemplate/list",
            params={"templateName": self.access_template_name},
        ), "业务接入模板")
        candidates = [
            row for row in templates if row.get("templateName") == self.access_template_name
        ]
        if not candidates:
            self._backup("access-template-before", {"templates": templates})
            self._api("POST", "/admin/bizAccessTemplate", body={
                "templateName": self.access_template_name, "remark": ARCHIVE_DESCRIPTION,
            })
            candidates = [row for row in self._list(self._api(
                "GET", "/admin/bizAccessTemplate/list",
                params={"templateName": self.access_template_name},
            ), "业务接入模板") if row.get("templateName") == self.access_template_name]
        template = self._one(candidates, "新闻网专用业务接入模板")
        template_id = template.get("id")
        if not isinstance(template_id, int) or template_id <= 0:
            raise ArchiveInitializationError("业务接入模板编号不合法")
        fields = self._list(self._api(
            "GET", "/admin/bizAccessField/list", params={"templateId": template_id},
        ), "业务接入字段映射")
        fields = [row for row in fields if row.get("templateId") == template_id]
        names = [str(row.get("fieldName", "")).upper() for row in fields]
        if len(set(names)) != len(names):
            raise ArchiveInitializationError("新闻网专用模板中存在重复字段映射")
        by_name = dict(zip(names, fields))
        expected_fields = {}
        for order, (name, source) in enumerate(SOURCE_FIELDS.items(), start=1):
            expected = {
                "templateId": template_id, "fieldName": name,
                "fieldCName": source_fields[name].get("fieldCName") or source,
                "accessFieldName": source, "accessFieldCName": source, "orderd": order,
            }
            if name == "CJR":
                expected.update({"convertType": "fixed", "convertValue": "档案馆"})
            elif name == "QZH":
                expected.update({"convertType": "fixed", "convertValue": qzh_value})
            else:
                expected.update({"convertType": None, "convertValue": None})
            existing = by_name.get(name)
            if existing and any(existing.get(key) != expected[key] for key in (
                    "accessFieldName", "convertType", "convertValue")):
                # 空转换参数与未设置等价；不把旧的固定值或枚举映射悄悄改为直取。
                if not (name not in ("CJR", "QZH") and existing.get("accessFieldName") == source
                        and not existing.get("convertType") and not existing.get("convertValue")):
                    raise ArchiveInitializationError("本站已有不同字段映射，禁止覆盖：" + name)
            expected_fields[name] = expected
        if set(by_name) - set(expected_fields):
            raise ArchiveInitializationError("本站专用模板含有方案外的映射，须先核对影响范围")
        missing_fields = [row for name, row in expected_fields.items() if name not in by_name]
        if missing_fields:
            self._backup("access-fields-before", {"template": template, "fields": fields})
            for row in missing_fields:
                self._api("POST", "/admin/bizAccessField", body=row)
            fields = self._list(self._api(
                "GET", "/admin/bizAccessField/list", params={"templateId": template_id},
            ), "业务接入字段映射")
            by_name = {}
            for row in fields:
                name = str(row.get("fieldName", "")).upper()
                if name in by_name or row.get("templateId") != template_id:
                    raise ArchiveInitializationError("字段映射回读存在重复或模板身份不一致")
                by_name[name] = row
            for name, expected in expected_fields.items():
                row = by_name.get(name, {})
                if (row.get("accessFieldName") != expected["accessFieldName"]
                        or (row.get("convertType") or None) != expected["convertType"]
                        or (row.get("convertValue") or None) != expected["convertValue"]):
                    raise ArchiveInitializationError("字段映射回读不一致：" + name)
        required = {"ZTM": "标题", "WZ": "网址", "ND": "年度", "JTXWNR": "具体新闻内容"}
        for name, source in required.items():
            field = by_name.get(name)
            if not field or field.get("accessFieldName") != source or field.get("convertType"):
                raise ArchiveInitializationError(f"专用模板必须配置 {source} → {name} 的直接映射")
        classification = by_name.get("FLH")
        if not classification:
            raise ArchiveInitializationError("专用模板缺少分类号 → FLH 映射，请先配置当前站点的专用字段模板")
        if classification.get("accessFieldName") == "分类号" and not classification.get("convertType"):
            pass
        elif (classification.get("accessFieldName") == "所属栏目"
              and classification.get("convertType") == "enum"):
            try:
                values = json.loads(classification.get("convertValue") or "{}")
            except (TypeError, ValueError):
                raise ArchiveInitializationError("分类号枚举映射不是有效 JSON") from None
            if any(values.get(name) != code for name, code in self.categories.items()):
                expected = "、".join(f"{name} → {code}" for name, code in self.categories.items())
                raise ArchiveInitializationError("分类号枚举必须对应当前归档栏目：" + expected)
        else:
            raise ArchiveInitializationError("专用模板分类号转换配置不符合当前归档栏目要求")
        return template_id, by_name

    def _configure_layout(self, table_name, fields):
        """仅更新本站目标库的显隐和顺序，结构、默认值及隐藏业务规则保持现场配置。"""
        updates = []
        expected_by_name = {}
        if "ID" in fields:
            # 系统主键保留结构，但不能额外占用方案约定的列表、编辑和查询显示项。
            expected_by_name["ID"] = {"hideListFlag": 1, "hideEditFlag": 1, "hideSelectFlag": 1}
            if any(fields["ID"].get(key) != value for key, value in expected_by_name["ID"].items()):
                updates.append({"id": fields["ID"]["id"], "tableName": table_name, **expected_by_name["ID"]})
        for order, name in enumerate(ARCHIVE_FIELDS, start=1):
            row = fields[name]
            expected = {
                "hideListFlag": 0 if name in LIST_FIELDS else 1,
                "hideEditFlag": 0 if order <= 18 else 1,
                "hideSelectFlag": 0 if order <= 18 and name not in ("QZH", "JH") else 1,
                "orderNumEdit": order,
                "orderNumList": LIST_FIELDS.index(name) + 1 if name in LIST_FIELDS else order + 10,
            }
            if name in ("QZH", "JH"):
                expected["disEditFlag"] = 1
            expected_by_name[name] = expected
            if any(row.get(key) != value for key, value in expected.items()):
                updates.append({"id": row["id"], "tableName": table_name, **expected})
        if updates:
            self._backup("library-fields-before", fields)
            if self._fields(table_name) != fields:
                raise ArchiveInitializationError("目标库字段在备份后发生并发修改，已停止配置")
            self._api("POST", "/library/table/field/updateBatch",
                      params={"personalFlag": "0"}, body=updates)
            confirmed = self._fields(table_name)
            if self._structure(confirmed) != self._structure(fields):
                raise ArchiveInitializationError("字段布局更新影响了业务结构，请保留备份并检查")
            for name, expected in expected_by_name.items():
                if any(confirmed[name].get(key) != value for key, value in expected.items()):
                    raise ArchiveInitializationError("字段布局回读不一致：" + name)
        return {"businessFields": 38, "listVisible": 10, "editVisible": 18, "queryVisible": 16}

    def _library_nodes(self):
        result = self._api("GET", "/library/table/treeList/ER")
        if not isinstance(result, dict):
            raise ArchiveInitializationError("电子档案库树接口返回结构不符合预期")
        roots = self._list(result.get("treeList"), "电子档案库树")
        nodes = []

        def visit(rows):
            for row in rows:
                if row.get("type") == "table":
                    nodes.append(row)
                children = row.get("children")
                if children:
                    visit(self._list(children, "电子档案库子节点"))

        visit(roots)
        return nodes

    @staticmethod
    def _structure(fields):
        """比较字段类型及业务规则，忽略创建时间、主键和页面排序。"""
        keys = ("typed", "lengthd", "numericScale", "nullFlag", "initType", "initValue", "fieldRule")
        return {name: {key: row.get(key) for key in keys} for name, row in fields.items()}

    def _identity(self):
        return {
            "baseUrl": self.base_url, "parentId": self.parent_id, "fondId": self.fond_id,
            "sourceTemplateId": self.template_table_id, "libraryName": self.library_name,
            "targetLibraryId": self.library_table_id,
            "accessTemplateName": self.access_template_name, "dataStatus": self.data_status,
        }

    def _load_state(self):
        if not self.state_file.exists():
            return None
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ArchiveInitializationError("归档初始化状态文件无法读取，请保留原文件并检查") from None
        if not isinstance(state, dict) or state.get("identity") != self._identity():
            raise ArchiveInitializationError("初始化状态文件与当前现场配置不同，请使用独立状态文件")
        return state

    def _save_state(self, state):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(self.state_file.name + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.state_file)

    def initialize(self):
        """初始化前完成只读校验，并在每步完成后记录可恢复的业务身份。"""
        state = self._load_state()
        if self.parent_id != -1:
            parent = self._table(self.parent_id)
            if parent.get("tableTypeCode") != "ER" or parent.get("relateType"):
                raise ArchiveInitializationError("父节点必须是电子档案归档库中的目录节点")
            if parent.get("fondId") not in (None, self.fond_id):
                raise ArchiveInitializationError("父节点所属全宗与 ERMS_FOND_ID 不一致")
        template = self._table(self.template_table_id)
        if template.get("relateType") != "WJM" or not template.get("wjTableName"):
            raise ArchiveInitializationError("请选取已定义字段的按文件管理网页采集模板库")
        if template.get("fondId") not in (None, self.fond_id):
            raise ArchiveInitializationError("模板库所属全宗与 ERMS_FOND_ID 不一致")
        source_fields = self._fields(template["wjTableName"])
        if set(source_fields) - {"ID"} != set(ARCHIVE_FIELDS):
            raise ArchiveInitializationError("请先选定包含方案中完整38项业务字段的网页模板库")
        if (source_fields["DH"].get("initType") != "comb"
                or source_fields["DH"].get("initValue") != "[QZH]-[ND]-[FLH]-[JH]"):
            raise ArchiveInitializationError("模板档号组合规则应为 [QZH]-[ND]-[FLH]-[JH]")
        import re
        if (source_fields["JH"].get("initType") != "comb"
                or not re.fullmatch(r"\$max\[\d+\]\(QZH,\s*ND,\s*FLH\)",
                                    source_fields["JH"].get("initValue") or "")):
            raise ArchiveInitializationError("模板件号须沿用按全宗号、年度、分类号分组递增的规则")
        source_structure = self._structure(source_fields)
        fingerprint = hashlib.sha256(json.dumps(
            source_structure, sort_keys=True, ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        if state and state.get("sourceStructureSha256") != fingerprint:
            raise ArchiveInitializationError("模板库字段结构已变化，请先检查现有归档库兼容性")

        if self.library_table_id is not None:
            selected = self._table(self.library_table_id)
            if (selected.get("tableTypeCode") != "ER" or selected.get("relateType") != "WJM"
                    or selected.get("pid") != self.parent_id or selected.get("fondId") != self.fond_id
                    or selected.get("wjTableName") != f"d_wj_{self.library_table_id}"):
                raise ArchiveInitializationError("指定归档库的编号、父节点、全宗、管理方式或动态表名不一致")
            matches = [{"tableId": self.library_table_id}]
        else:
            nodes = self._library_nodes()
            matches = [node for node in nodes
                       if node.get("pid") == self.parent_id and node.get("title") == self.library_name]
        bindings = self._list(self._api("GET", "/admin/bizAccess/list", params={
            "clientKey": "spider_news", "apiKey": "spider_user",
        }), "新闻采集接入配置")
        for category in self.categories:
            configured = [row for row in bindings if row.get("accessBI") == category]
            if len(configured) > 1:
                raise ArchiveInitializationError(f"栏目 {category} 已有重复接入配置，禁止继续初始化")
            if configured and not matches:
                raise ArchiveInitializationError(f"栏目 {category} 已绑定其他库，禁止创建重复归档库")
            if self.library_table_id is not None and configured and (
                    configured[0].get("tableId") != self.library_table_id
                    or configured[0].get("tableName") != f"d_wj_{self.library_table_id}"):
                raise ArchiveInitializationError(f"栏目 {category} 已绑定其他归档库，禁止覆盖")
        if len(matches) > 1:
            raise ArchiveInitializationError("同一父节点下存在多个同名归档库，禁止重复创建")
        # 模板及既有绑定的只读兼容检查完成后，才允许建立接收字段模板。
        access_template_id, mapping = self._mapping(source_fields)
        missing = set(mapping) - set(source_fields)
        if missing:
            raise ArchiveInitializationError("网页采集模板缺少字段：" + "、".join(sorted(missing)))
        if not matches:
            if self.library_table_id is not None:
                raise ArchiveInitializationError("指定归档库不存在，禁止按名称新建替代库")
            if state:
                raise ArchiveInitializationError("状态文件指定的归档库已不存在，禁止自动创建替代库")
            maximum = self._api("GET", "/library/table/getMaxOrderd", params={
                "tableTypeCode": "ER", "pid": self.parent_id,
            })
            self._api("POST", "/library/table", params={"templateTableId": self.template_table_id}, body={
                "tableCName": self.library_name, "description": ARCHIVE_DESCRIPTION,
                "pid": self.parent_id, "tableTypeCode": "ER", "relateType": "WJM",
                "fondId": self.fond_id, "orderd": int(maximum or 0) + 1, "erOpen": -1,
            })
            matches = [node for node in self._library_nodes()
                       if node.get("pid") == self.parent_id and node.get("title") == self.library_name]
        node = self._one(matches, "新闻网归档库")
        library_id = node.get("tableId") or node.get("id")
        library = self._table(library_id)
        if (library.get("tableTypeCode") != "ER" or library.get("relateType") != "WJM"
                or library.get("pid") != self.parent_id or library.get("fondId") != self.fond_id
                or (self.library_table_id is None and library.get("description") != ARCHIVE_DESCRIPTION)
                or (self.library_table_id is not None and (
                    library_id != self.library_table_id or library.get("wjTableName") != f"d_wj_{self.library_table_id}"))):
            raise ArchiveInitializationError("同名归档库的身份或管理方式不一致，禁止改写现场库")
        if state and state.get("libraryId") != library_id:
            raise ArchiveInitializationError("现有归档库编号与初始化状态文件不一致")
        table_name = library.get("wjTableName")
        if not table_name:
            raise ArchiveInitializationError("归档库尚未完成动态表创建，请检查 ERMS 服务日志")
        target_fields = self._fields(table_name)
        if self._structure(target_fields) != source_structure:
            raise ArchiveInitializationError("现有归档库与模板字段不一致，请检查建库或引用模板结果")
        layout = self._configure_layout(table_name, target_fields)
        statuses = self._list(self._api(
            "GET", f"/library/table/field/{library_id}/dataStatusMapping",
        ), "归档库数据状态")
        if not any(row.get("dataStatus") == self.data_status for row in statuses):
            raise ArchiveInitializationError("ERMS_DATA_STATUS 不属于当前归档库，请填写实际数据状态")
        state = {
            "identity": self._identity(), "libraryId": library_id, "tableName": table_name,
            "accessTemplateId": access_template_id, "sourceStructureSha256": fingerprint,
            "fieldLayout": layout,
            "categories": state.get("categories", {}) if state else {},
        }
        self._save_state(state)

        tags = self._list(self._api(
            "GET", "/library/table/tagListByTableId", params={"tableId": library_id},
        ), "归档栏目标签")
        for order, (category, classification) in enumerate(self.categories.items(), start=1):
            candidates = [row for row in tags if row.get("tagName") == category and row.get("pid") == -1]
            if len(candidates) > 1:
                raise ArchiveInitializationError(f"栏目 {category} 存在多个同名根标签")
            if not candidates:
                self._api("POST", "/library/table/tag", body={
                    "tableId": library_id, "pid": -1, "tagName": category, "orderd": order,
                })
                tags = self._list(self._api(
                    "GET", "/library/table/tagListByTableId", params={"tableId": library_id},
                ), "归档栏目标签")
                candidates = [row for row in tags if row.get("tagName") == category and row.get("pid") == -1]
            tag = self._one(candidates, f"栏目 {category} 标签")
            existing = state["categories"].get(category)
            if existing and existing.get("tagId") != tag.get("id"):
                raise ArchiveInitializationError(f"栏目 {category} 标签身份与状态文件不一致")
            expected = {
                "clientKey": "spider_news", "apiKey": "spider_user", "tableId": library_id,
                "tagId": tag["id"], "tableName": table_name, "dataStatus": self.data_status,
                "accessSSID": "SPIDER", "accessBI": category, "templateId": access_template_id,
            }
            configured = [row for row in bindings if row.get("accessBI") == category]
            if len(configured) > 1 or (configured and any(
                configured[0].get(key) != value for key, value in expected.items()
            )):
                raise ArchiveInitializationError(f"栏目 {category} 已有不同的接入配置，禁止覆盖")
            if not configured:
                self._api("POST", "/admin/bizAccess", body=expected)
                # 写入后必须回读，不能把成功响应直接当成配置已落库。
                confirmed = self._list(self._api("GET", "/admin/bizAccess/list", params={
                    "clientKey": "spider_news", "apiKey": "spider_user", "accessBI": category,
                }), "栏目接入配置")
                record = self._one([row for row in confirmed if row.get("accessBI") == category],
                                   f"栏目 {category} 接入配置")
                if any(record.get(key) != value for key, value in expected.items()):
                    raise ArchiveInitializationError(f"栏目 {category} 接入配置回读不一致")
                bindings.append(record)
            state["categories"][category] = {"tagId": tag["id"], "classification": classification}
            self._save_state(state)
        return state


def initialize_archive():
    """提供给采集程序的显式现场初始化入口。"""
    return ArchiveInitializer().initialize()
