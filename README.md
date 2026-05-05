# News Spider

高校/机构新闻爬虫通用模板，支持列表页分页采集、详情页内容提取、图片/视频下载、PDF 生成及 MySQL 入库。

## 架构概览

```
MY.py                          # 入口，编排采集流程
├── CollectLinks.py            # 列表页采集器：分页遍历 → 提取链接/标题 → 分发详情采集
│   └── CollectDetail.py      # 详情页采集器：提取标题/正文/图片/视频/元数据 → 入库
│       ├── WeChatContentExtractor.py   # 微信公众号内容专用提取器
│       ├── ImprovedPDFGenerator.py     # Playwright PDF 生成器
│       └── SqlClient.py               # MySQL 数据库操作
├── CollectCXWH.py             # 自定义采集器模板（处理多结构子源的复杂栏目）
└── ConfigManager.py           # 配置管理器：URL、XPath、栏目参数集中管理
```

## 文件说明

| 文件 | 职责 | 修改频率 |
|------|------|----------|
| `MY.py` | 主入口，按顺序调用各采集器 | 换站点时需修改 |
| `ConfigManager.py` | 栏目配置（URL、XPath、分页参数等） | 换站点时重点修改 |
| `CollectLinks.py` | 通用列表页采集：获取总页数、遍历分页、提取链接和标题 | 通常无需修改 |
| `CollectDetail.py` | 通用详情页采集：提取标题/正文/元数据、下载图片/视频、生成PDF、入库 | 通常无需修改 |
| `CollectCXWH.py` | 复杂栏目专用采集器模板（支持单页面、列表页、混合结构） | 有复杂栏目时参考修改 |
| `WeChatContentExtractor.py` | 微信公众号文章内容提取（标题/正文/图片） | 无需修改 |
| `ImprovedPDFGenerator.py` | 基于 Playwright 的 PDF 生成器（微信文章特殊处理） | 通常无需修改 |
| `SqlClient.py` | MySQL 数据库连接与 CRUD 操作 | 换数据库时修改连接信息 |
| `requirements.txt` | Python 依赖清单 | 添加新依赖时更新 |

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 安装 Playwright 浏览器（PDF 生成需要）
playwright install chromium
```

### 2. 配置数据库

编辑 `SqlClient.py`，修改数据库连接信息：

```python
conn = pymysql.connect(
    host='***',
    port=3306,
    user='***',
    passwd='***',
    db='***',
    use_unicode=True,
    charset="utf8"
)
```

### 3. 配置目标站点

编辑 `ConfigManager.py`，在 `categories_config` 字典中配置栏目。支持以下配置项：

#### 基础配置

| 字段 | 类型 | 说明 |
|------|------|------|
| `base_url` | `str` 或 `list[str]` | 栏目列表页 URL，多 URL 时传列表 |
| `base_domain` | `str` | 站点根域名（用于拼接相对路径） |
| `items_per_page` | `int` | 每页显示条数（默认 15） |
| `is_active` | `bool` | 是否启用采集 |

#### XPath 配置

| 字段 | 类型 | 说明 |
|------|------|------|
| `total_count_xpath` | `list[str]` | 总条数 XPath（按优先级排列） |
| `news_links_xpath` | `list[str]` | 新闻链接 XPath |
| `news_titles_xpath` | `list[str]` | 新闻标题 XPath |
| `title_xpath` | `list[str]` | 详情页标题 XPath |
| `date_xpath` | `list[str]` | 详情页日期 XPath |
| `author_xpath` | `list[str]` | 详情页作者 XPath |
| `content_xpath` | `list[str]` | 详情页正文 XPath |
| `image_xpath` | `list[str]` | 详情页图片 XPath |
| `video_xpath` | `list[str]` | 详情页视频 XPath |

#### 高级配置

| 字段 | 类型 | 说明 |
|------|------|------|
| `valid_url_patterns` | `list[str]` | 有效链接域名白名单 |
| `special_handling` | `dict` | 特殊处理配置 |

`special_handling` 支持的子字段：

| 字段 | 说明 |
|------|------|
| `metadata_xpath` | 元数据区域 XPath（来源/作者/日期/审核） |
| `use_list_title` | 是否优先使用列表页标题 |
| `link_filter_pattern` | 链接过滤模式（只采集匹配的链接） |
| `multiple_base_urls` | 标记为多 URL 栏目 |
| `use_custom_collector` | 是否使用专用采集器 |
| `custom_collector` | 专用采集器模块名 |
| `sub_sources` | 子源配置（复杂栏目专用，参见 `CollectCXWH.py`） |

### 4. 配置示例

#### 标准列表页栏目

```python
"新闻栏目": {
    "base_url": "https://example.edu.cn/index/news.htm",
    "base_domain": "https://example.edu.cn",
    "items_per_page": 15,
    "is_active": True,
    "total_count_xpath": ["//div[@class='pb_sys_common']/span[2]/text()"],
    "news_links_xpath": ["//ul[@class='list']//a/@href"],
    "news_titles_xpath": ["//ul[@class='list']//a/@title"],
    "title_xpath": ["//h1/text()", "//title/text()"],
    "date_xpath": ["//div/form/p//text()"],
    "author_xpath": ["//div/form/p//text()"],
    "content_xpath": ["//div[@class='v_news_content']//text()"],
    "image_xpath": ["//div[@class='v_news_content']//img/@src"],
    "video_xpath": ["//div[@class='v_news_content']//video/source/@src"],
    "valid_url_patterns": ["https://example.edu.cn", "https://mp.weixin.qq.com"],
    "special_handling": {
        "metadata_xpath": "//div/form/p"
    }
}
```

#### 单页面栏目（无分页）

```python
"单页栏目": {
    "urls": ["https://example.edu.cn/about.htm"],
    "is_single_page": True,
    "detail_config": {
        "title_xpath": "//div[@class='header']/h2/text()",
        "content_xpath": "//div[@id='content']//text()",
        "image_xpath": "//div[@id='content']//img/@src",
        "video_xpath": None,
        "metadata_xpath": None
    }
}
```

### 5. 运行

```bash
# 直接运行
python MY.py

# 后台运行（Linux）
nohup python MY.py > news.log 2>&1 &
```

## 数据流

```
列表页 URL
  │
  ▼
获取总页数 (total_count_xpath)
  │
  ▼
遍历分页 (倒序分页算法)
  │
  ▼
提取链接 + 标题 (news_links_xpath / news_titles_xpath)
  │
  ▼
过滤链接 (valid_url_patterns / link_filter_pattern)
  │
  ├─ 无效链接 → 记录到 invalid_links.txt 及数据库 (is_valid_link 标记)
  │
  └─ 有效链接 → CollectDetail.Collect_dea()
                   │
                   ├─ 微信公众号 URL → WeChatContentExtractor
                   └─ 普通 URL → XPath 提取
                   │
                   ├─ 提取标题 / 元数据（来源/作者/日期/审核）
                   ├─ 提取正文内容
                   ├─ 下载图片（MD5 哈希命名）
                   ├─ 下载视频
                   ├─ 生成 PDF（Playwright）
                   ├─ 保存 JSON 文件
                   └─ 插入 MySQL 数据库
```

## 数据库表结构

```sql
CREATE TABLE news_spider (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(500),
    url VARCHAR(500),
    is_valid_link VARCHAR(200) DEFAULT NULL COMMENT 'NULL=有效, 非NULL=无效原因',
    record_id INT DEFAULT NULL COMMENT '-1=无效链接',
    release_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    dict_ TEXT COMMENT 'JSON格式的完整数据',
    image_path_list TEXT COMMENT '图片路径JSON数组',
    video_path_list TEXT COMMENT '视频路径JSON数组',
    pdf_path TEXT COMMENT 'PDF路径JSON数组',
    info TEXT,
    error_json INT DEFAULT 0,
    UNIQUE KEY idx_url (url)
);
```

## 自定义采集器

当目标栏目结构复杂（如包含单页面、不同 XPath 布局、嵌套子源），可在 `ConfigManager.py` 中设置 `use_custom_collector: True` 并参照 `CollectCXWH.py` 编写专用采集器。

专用采集器需支持两种页面类型：
- **列表页** (`is_single_page: false`)：支持分页遍历，逐条调用 `CollectDetail.Collect_dea()`
- **单页面** (`is_single_page: true`)：直接提取内容并入库

## 分页说明

本模板采用**倒序分页**算法，适配部分高校 CMS 的分页格式：

```
第1页 → /news.htm
第2页 → /news/195.htm  (total_pages - 1)
第3页 → /news/194.htm  (total_pages - 2)
...
最后一页 → /news/1.htm
```

如目标站点使用其他分页格式（如 `?page=2`），修改 `CollectLinks._get_page_url()` 方法即可。

## 注意事项

- `SqlClient.py` 中的数据库密码为敏感信息，部署时注意权限控制
- 图片和视频下载使用流式传输，大文件下载超时时间较长（600s）
- PDF 生成依赖 Playwright，首次使用需安装 Chromium 浏览器
- 微信公众号 URL 会自动切换至专用提取器处理
- 建议设置合理的 `time.sleep()` 间隔，避免请求过快被封
