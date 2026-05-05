#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改进的PDF生成器 - 基于web_to_pdf_converter.py的优秀特性
支持微信公众号、学校官网等多种网站的PDF生成
"""

import os
import sys
import json
import time
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse
import hashlib

try:
    from playwright.async_api import async_playwright, Browser, Page
except ImportError:
    print("请先安装playwright: pip install playwright")
    print("然后安装浏览器: playwright install chromium")
    sys.exit(1)

class ImprovedPDFGenerator:
    """改进的PDF生成器"""
    
    def __init__(self, output_dir: str = "./pdf_output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        # 配置参数
        self.timeout = 30000
        self.wait_time = 5000
        self.max_retries = 3
        self.headless = True
        
        self.setup_logging()
        
        # 网站特定配置 - 只针对微信公众号做特殊处理
        self.wechat_config = {
            'wait_selector': '.rich_media_content, #js_content, .rich_media_area_primary',
            'remove_selectors': ['.ad', '.advertisement', '.sidebar', '.comment-box', '.rich_media_tool'],
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'timeout': 90000,  # 微信公众号需要更长时间
            'image_wait_time': 45000,  # 图片加载等待时间
            'need_scroll': True  # 需要滚动加载图片
        }
    
    def setup_logging(self):
        """设置日志"""
        log_dir = Path('./logs')
        log_dir.mkdir(exist_ok=True)
        
        log_file = log_dir / f'pdf_generator_{datetime.now().strftime("%Y%m%d")}.log'
        
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)
    
    def is_wechat_url(self, url: str) -> bool:
        """检查是否为微信公众号URL"""
        return 'mp.weixin.qq.com' in url
    
    def generate_filename(self, title: str) -> str:
        """
        生成PDF文件名 - 兼容原先ToPDF的哈希命名方式
        :param title: 标题
        :return: 哈希文件名
        """
        # 使用MD5哈希生成文件名，与原先ToPDF保持一致
        title_md5 = hashlib.md5(title.encode('utf-8')).hexdigest()
        return f"{title_md5}.pdf"
    
    async def wait_for_images_to_load(self, page: Page, max_wait_time: int = 30000):
        """等待图片加载完成 - 从web_to_pdf_converter.py学习的核心功能"""
        self.logger.info("开始等待图片加载...")
        
        try:
            # 先等待一下让页面稳定
            await page.wait_for_timeout(2000)
            
            # 滚动页面以触发懒加载图片
            await page.evaluate("""
                () => {
                    return new Promise((resolve) => {
                        let totalHeight = 0;
                        let distance = 100;
                        let timer = setInterval(() => {
                            let scrollHeight = document.body.scrollHeight;
                            window.scrollBy(0, distance);
                            totalHeight += distance;
                            
                            if(totalHeight >= scrollHeight){
                                clearInterval(timer);
                                // 滚动回顶部
                                window.scrollTo(0, 0);
                                resolve();
                            }
                        }, 100);
                    });
                }
            """)
            
            # 等待图片加载完成
            start_time = asyncio.get_event_loop().time()
            while True:
                current_time = asyncio.get_event_loop().time()
                if (current_time - start_time) * 1000 > max_wait_time:
                    self.logger.warning(f"图片加载等待超时 ({max_wait_time}ms)")
                    break
                
                # 检查图片加载状态
                unloaded_images = await page.evaluate("""
                    () => {
                        const images = document.querySelectorAll('img');
                        let unloaded = 0;
                        let loading = 0;
                        
                        images.forEach(img => {
                            // 检查图片是否完全加载
                            if (!img.complete) {
                                unloaded++;
                            }
                            // 检查是否还在显示加载状态
                            if (img.src.includes('loading') || img.alt.includes('加载') || 
                                img.classList.contains('loading') || 
                                img.parentElement.classList.contains('loading')) {
                                loading++;
                            }
                        });
                        
                        return {
                            total: images.length,
                            unloaded: unloaded,
                            loading: loading
                        };
                    }
                """)
                
                self.logger.info(f"图片状态: 总数={unloaded_images['total']}, 未加载={unloaded_images['unloaded']}, 加载中={unloaded_images['loading']}")
                
                # 如果所有图片都加载完成
                if unloaded_images['unloaded'] == 0 and unloaded_images['loading'] == 0:
                    await page.wait_for_timeout(2000)  # 再等待2秒确保稳定
                    break
                
                await page.wait_for_timeout(1000)  # 每秒检查一次
            
            self.logger.info("图片加载等待完成")
            
        except Exception as e:
            self.logger.warning(f"图片加载等待过程中出现异常: {str(e)}")
            await page.wait_for_timeout(5000)  # 异常时等待5秒
    
    async def generate_pdf(self, url: str, save_path: str, title: str) -> Optional[str]:
        """
        生成PDF文件 - 对微信公众号做特殊处理，其他网站标准处理
        :param url: 网页URL
        :param save_path: 保存目录路径
        :param title: 网页标题（用于生成哈希文件名）
        :return: PDF文件路径，失败返回None
        """

        try:
            # 创建保存目录（如果不存在）
            os.makedirs(save_path, exist_ok=True)

            # 生成文件名（使用哈希命名）
            filename = self.generate_filename(title)
            filepath = os.path.join(save_path, filename)

            # 检查是否为微信公众号，决定使用哪种配置
            is_wechat = self.is_wechat_url(url)

            if is_wechat:
                self.logger.info(f"开始生成微信公众号PDF: {url} -> {filepath}")
                config = self.wechat_config
                timeout = config.get('timeout', self.timeout)
            else:
                self.logger.info(f"开始生成标准PDF: {url} -> {filepath}")
                # 标准网站配置（简单处理）
                config = {
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                    'timeout': 60000,
                    'need_scroll': False
                }
                timeout = config.get('timeout', self.timeout)
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)
                
                try:
                    # 创建浏览器上下文
                    context = await browser.new_context(
                        user_agent=config['user_agent'],
                        viewport={'width': 1920, 'height': 1080}
                    )
                    
                    page = await context.new_page()
                    
                    # 导航到页面
                    self.logger.info(f"导航到页面: {url}")
                    response = await page.goto(url, timeout=timeout, wait_until='domcontentloaded')

                    if not response or response.status >= 400:
                        raise Exception(f"页面加载失败，状态码: {response.status if response else 'None'}")

                    # 获取页面标题
                    page_title = await page.title()
                    self.logger.info(f"页面标题: {page_title}")

                    if is_wechat:
                        # 微信公众号特殊处理
                        self.logger.info("执行微信公众号特殊处理")

                        # 等待微信内容加载
                        try:
                            self.logger.info(f"等待微信内容选择器: {config['wait_selector']}")
                            await page.wait_for_selector(config['wait_selector'], timeout=self.wait_time)
                            self.logger.info("微信内容加载完成")
                        except:
                            self.logger.warning(f"等待微信内容超时: {config['wait_selector']}")

                        # 滚动加载图片
                        if config.get('need_scroll', False):
                            image_wait_time = config.get('image_wait_time', 45000)
                            await self.wait_for_images_to_load(page, image_wait_time)

                        # 移除微信公众号的广告和无关元素
                        for selector in config.get('remove_selectors', []):
                            try:
                                await page.evaluate(f"""
                                    document.querySelectorAll('{selector}').forEach(el => el.remove());
                                """)
                            except:
                                pass
                    else:
                        # 标准网站处理
                        self.logger.info("执行标准网站处理")

                        # 简单等待页面加载完成
                        try:
                            await page.wait_for_load_state('networkidle', timeout=10000)
                        except:
                            # 如果网络空闲等待失败，简单等待3秒
                            await page.wait_for_timeout(3000)

                    # 生成PDF - 统一的PDF配置
                    self.logger.info(f"开始生成PDF: {filepath}")

                    # 获取页面高度
                    body_height = await page.evaluate('document.body.scrollHeight')

                    # 统一的PDF选项（兼容原先ToPDF）
                    await page.pdf(
                        path=filepath,
                        format='A2',
                        print_background=True,
                        prefer_css_page_size=True,
                        margin={
                            'top': '0.5cm',
                            'right': '0.5cm',
                            'bottom': '0.5cm',
                            'left': '0.5cm'
                        },
                        display_header_footer=False
                    )

                    success_msg = f"{'微信公众号' if is_wechat else '标准'}PDF生成成功: {filepath}"
                    self.logger.info(success_msg)
                    return filepath
                    
                finally:
                    await browser.close()
                    
        except Exception as e:
            self.logger.error(f"PDF生成失败 {url}: {str(e)}")
            return None
