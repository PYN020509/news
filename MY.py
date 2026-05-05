from CollectLinks import NewsCollector  # 引入新闻采集器
from CollectCXWH import CXWHCollector  # 引入成信文化采集器
import time

if __name__ == '__main__':
    # 启动新闻采集
    print("=" * 80)
    print("成都信息工程大学新闻采集系统")
    print("=" * 80)
    print(f"开始时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
    print()

    # 第一步：采集标准栏目（成信要闻、综合新闻等7个栏目）
    print("\n" + "=" * 80)
    print("第一步：采集标准栏目")
    print("=" * 80)
    print("包括：成信要闻、综合新闻、媒体成信、两优一先、成信学术、成信融媒、成信人物")
    print()

    news_collector = NewsCollector()
    news_collector.collect_news()

    # 第二步：采集成信文化栏目
    print("\n" + "=" * 80)
    print("第二步：采集成信文化栏目")
    print("=" * 80)
    print("包括：成信印象系列、大学精神、学校学报、文化活动")
    print()

    cxwh_collector = CXWHCollector()
    cxwh_collector.collect_all()

    # 完成
    print("\n" + "=" * 80)
    print(f"所有采集任务完成: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
    print("=" * 80)
    print("\n采集统计：")
    print("  标准栏目：7个（成信要闻、综合新闻、媒体成信、两优一先、成信学术、成信融媒、成信人物）")
    print("  成信文化：7个子源（成信印象×4、大学精神、学校学报、文化活动）")
    print("  总计：8个栏目，14个采集源")
    print("=" * 80)