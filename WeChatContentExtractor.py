#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号内容提取器
基于batch_processor.py的微信公众号处理逻辑
"""

import re
import json
import requests
from lxml import etree
from urllib.parse import urljoin
import logging
from datetime import datetime
import time

class WeChatContentExtractor:
    """微信公众号内容提取器"""
    
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }
        
        # 微信公众号特定的xpath配置
        self.wechat_xpaths = {
            'title': [
                '//h1[@id="activity-name"]//text()',
                '//h1[@class="rich_media_title"]//text()',
                '//h2[@class="rich_media_title"]//text()',
                '//*[@id="activity-name"]/text()',
                '//title/text()'
            ],
            'date': [
                '//*[@id="publish_time"]/text()',
                '//em[@id="publish_time"]/text()',
                '//span[@class="rich_media_meta_text"]/text()',
                '//*[contains(@class, "publish_time")]/text()',
                '//*[contains(@class, "time")]/text()'
            ],
            'author': [
                '//*[@id="js_name"]/text()',
                '//a[@id="js_name"]/text()',
                '//*[contains(@class, "rich_media_meta_nickname")]/text()',
                '//*[contains(@class, "account_nickname")]/text()'
            ],
            'content': [
                '//*[@id="js_content"]//p//text()',
                '//*[@id="js_content"]//div//text()',
                '//*[@id="js_content"]//span//text()',
                '//div[@class="rich_media_content"]//p//text()',
                '//div[@class="rich_media_content"]//div//text()'
            ],
            'images': [
                '//*[@id="js_content"]//img/@data-src',
                '//*[@id="js_content"]//img/@src',
                '//div[@class="rich_media_content"]//img/@data-src',
                '//div[@class="rich_media_content"]//img/@src'
            ]
        }
    
    def is_wechat_url(self, url: str) -> bool:
        """判断是否为微信公众号URL"""
        return 'mp.weixin.qq.com' in url

    def extract_content(self, url: str) -> dict:
        """
        提取微信公众号内容
        :param url: 微信公众号文章URL
        :return: 提取的内容字典
        """
        if not self.is_wechat_url(url):
            return {'error': '不是微信公众号URL'}

        try:
            # 发送请求
            response = requests.get(url, headers=self.headers, timeout=30, verify=False)
            response.raise_for_status()
            response.encoding = 'utf-8'

            # 解析HTML
            html = etree.HTML(response.text)

            # 提取各项内容
            result = {
                'url': url,
                'title': self._extract_title(html),
                'date': self._extract_date(html),
                'author': self._extract_author(html),
                'content': self._extract_content_text(html),
                'images': self._extract_images(html, url),
                'raw_html': response.text,
                'extraction_time': datetime.now().isoformat()
            }

            return result

        except Exception as e:
            logging.error(f"微信公众号内容提取失败 {url}: {str(e)}")
            return {'error': str(e), 'url': url}
    
    def _extract_title(self, html) -> str:
        """提取标题"""
        for xpath in self.wechat_xpaths['title']:
            try:
                titles = html.xpath(xpath)
                if titles:
                    title = ''.join(titles).strip()
                    if title:
                        return title
            except:
                continue
        return ""
    
    def _extract_date(self, html) -> str:
        """提取发布时间"""
        for xpath in self.wechat_xpaths['date']:
            try:
                dates = html.xpath(xpath)
                if dates:
                    date_text = ''.join(dates).strip()
                    if date_text:
                        # 清理日期文本
                        date_text = re.sub(r'[^\d\-\s:年月日]', '', date_text)
                        return date_text
            except:
                continue
        return ""
    
    def _extract_author(self, html) -> str:
        """提取作者/公众号名称"""
        for xpath in self.wechat_xpaths['author']:
            try:
                authors = html.xpath(xpath)
                if authors:
                    author = ''.join(authors).strip()
                    if author:
                        return author
            except:
                continue
        return ""
    
    def _extract_content_text(self, html) -> list:
        """提取正文内容"""
        content_list = []
        
        for xpath in self.wechat_xpaths['content']:
            try:
                texts = html.xpath(xpath)
                if texts:
                    # 清理和过滤文本
                    cleaned_texts = []
                    for text in texts:
                        text = text.strip()
                        if text and len(text) > 1:  # 过滤掉空白和单字符
                            # 过滤掉一些无用的文本
                            if not any(skip in text for skip in ['微信号', '功能介绍', '收录于话题', '阅读原文']):
                                cleaned_texts.append(text)
                    
                    if cleaned_texts:
                        content_list.extend(cleaned_texts)
                        break  # 找到内容就停止
            except:
                continue
        
        return content_list
    
    def _extract_images(self, html, base_url: str) -> list:
        """提取图片URL"""
        image_urls = []
        
        for xpath in self.wechat_xpaths['images']:
            try:
                urls = html.xpath(xpath)
                if urls:
                    for url in urls:
                        if url and url.startswith('http'):
                            # 微信图片URL通常需要特殊处理
                            if 'mmbiz.qpic.cn' in url or 'mmbiz.qlogo.cn' in url:
                                image_urls.append(url)
                            elif url.startswith('//'):
                                image_urls.append('https:' + url)
                            else:
                                image_urls.append(url)
            except:
                continue
        
        # 去重
        return list(set(image_urls))
    
    def extract_batch(self, urls: list) -> list:
        """
        批量提取微信公众号内容
        :param urls: URL列表
        :return: 提取结果列表
        """
        results = []
        
        for i, url in enumerate(urls, 1):
            print(f"提取进度: {i}/{len(urls)} - {url}")
            
            result = self.extract_content(url)
            results.append(result)
            
            # 添加延迟避免被限制
            if i < len(urls):
                time.sleep(2)
        
        return results
    
    def format_for_database(self, extracted_data: dict, category: str = "微信公众号") -> dict:
        """
        将提取的数据格式化为数据库存储格式
        :param extracted_data: 提取的原始数据
        :param category: 分类名称
        :return: 格式化后的数据
        """
        if 'error' in extracted_data:
            return {
                'error': extracted_data['error'],
                'url': extracted_data.get('url', '')
            }
        
        # 格式化为与现有系统兼容的格式
        formatted_data = {
            "网站名称": "微信公众号",
            "网站域名": "https://mp.weixin.qq.com",  # 微信公众号域名
            "网址": extracted_data['url'],  # 完整URL
            "所属栏目": category,
            "标题": extracted_data['title'],
            "发布时间": extracted_data['date'],
            "来源": "微信公众号",
            "作者": extracted_data['author'],
            "审核": "",
            "具体新闻内容": extracted_data['content'],
            "采集人": "档案馆"
        }

        return formatted_data

# 使用示例
def test_wechat_extractor():
    """测试微信公众号提取器"""
    extractor = WeChatContentExtractor()
    
    # 测试URL（需要替换为实际的微信公众号文章URL）
    test_url = "https://mp.weixin.qq.com/s/example"
    
    print("开始测试微信公众号内容提取...")
    result = extractor.extract_content(test_url)
    
    if 'error' in result:
        print(f"提取失败: {result['error']}")
    else:
        print(f"标题: {result['title']}")
        print(f"作者: {result['author']}")
        print(f"发布时间: {result['date']}")
        print(f"内容段落数: {len(result['content'])}")
        print(f"图片数量: {len(result['images'])}")
        
        # 格式化为数据库格式
        formatted = extractor.format_for_database(result)
        print("\n格式化后的数据:")
        print(json.dumps(formatted, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    test_wechat_extractor()
