# -*- coding: utf-8 -*-
"""本地验证：用 mock 数据测试过滤 + HTML 渲染 + 发件人名称（不依赖网络）"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from henan_monitor import apply_filter, render_email, KEYWORDS, TYPE_ALLOW, EXCLUDE_WORDS
from datetime import datetime, timedelta

# mock 抓取数据
now = datetime.now()
mock_items = [
    {"hash": "1", "title": "郑州市财政局工程造价咨询服务项目-公开招标公告", "link": "https://example.com/1", "pub": now.strftime("%Y-%m-%d %H:%M")},
    {"title": "洛阳市审计局财务审计服务采购-竞争性磋商公告", "link": "https://example.com/2", "pub": now.strftime("%Y-%m-%d")},
    {"title": "XX项目预算评审服务-单一来源公示", "link": "https://example.com/3", "pub": now.strftime("%Y-%m-%d")},
    {"title": "XX空调设备采购废标公告", "link": "https://example.com/4", "pub": now.strftime("%Y-%m-%d")},  # 应被排除
    {"title": "XX服务器采购项目-询价公告", "link": "https://example.com/5", "pub": now.strftime("%Y-%m-%d")},  # 不含关键词，过滤掉
]

today = now
start = today - timedelta(days=7)
filtered = apply_filter(mock_items, today, start)
print(f"过滤前: {len(mock_items)} 条")
print(f"过滤后: {len(filtered)} 条（应=3：含造价/审计/评审 + 招标/磋商/单一来源，排除废标）")
for it in filtered:
    print(f"  ✅ [{it['pub']}] {it['title']}")

html, mime = render_email(filtered)
print(f"\n渲染类型: {mime}")
print(f"HTML 长度: {len(html)} 字符")
print("HTML 前 300 字符预览:")
print(html[:300])

# 验证发件人名称逻辑
from email.utils import formataddr
name = "📢 河南招标监控"
addr = "test@126.com"
print(f"\n发件人格式: {formataddr((name, addr))}")
print("\n✅ 本地验证完成。确认无误后：")
print("   1) 填好 EMAIL_USER/EMAIL_PASS/EMAIL_TO 等环境变量")
print("   2) python henan_monitor.py   # 真实抓取+推送")
