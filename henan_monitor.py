# -*- coding: utf-8 -*-
"""
河南政府采购网 监控 v2.0.0（云端版，含网络重试）
- requests 抓静态 HTML（div.List2 ul li）
- 关键词 ∩ 类型 双重过滤
- HTML 邮件（jinja2 模板，自定义发件人名称）
- 企业微信群机器人推送
- ★ 网络抖动自动重试（fetch_channel 单次请求失败时重试 3 次）
- 设计借鉴 NodeMail（https://github.com/lolo0606/NodeMail）

环境变量（GitHub Actions Secrets）：
    EMAIL_USER    发件箱地址
    EMAIL_PASS    SMTP 授权码（非登录密码）
    EMAIL_TO      收件人，多个用逗号分隔
    SMTP_SERVER   smtp.126.com / smtp.qq.com
    SMTP_PORT     465（SSL）或 587（STARTTLS）
    SENDER_NAME   自定义发件人显示名
    WECOM_WEBHOOK 企业微信群机器人完整 URL
"""
import os
import time
import hashlib
import sqlite3
import smtplib
import requests
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.utils import formataddr

try:
    from jinja2 import Template
except ImportError:
    Template = None

from bs4 import BeautifulSoup

# ==================== 配置区 ====================
VERSION = "2.0.0"

KEYWORDS = [""造价", "审计", "评审"]   # 调试用；正式可改回 ["造价", "审计", "评审"]
TYPE_ALLOW = ["招标", "征集", "磋商", "采购", "谈判", "询价", "单一来源", "资格预审"]
EXCLUDE_WORDS = ["废标", "终止", "合同公告", "验收", "中标公告", "成交公告"]

TARGET_CHANNELS = [
    {"name": "省级-采购公告",   "url": "https://zfcg.henan.gov.cn/henan/list2?channelCode=0101&bz=1&pageNo=1&pageSize=16&gglx=0"},
    {"name": "市州县-采购公告", "url": "https://zfcg.henan.gov.cn/henan/list2?channelCode=0101&bz=2&pageNo=1&pageSize=16&gglx=0"},
]

DAYS_BACK = 7

# ---- 网络重试配置 ----
FETCH_RETRIES = 3      # 单次抓取最大重试次数
FETCH_DELAY   = 5       # 每次重试间隔（秒）

# ---- 环境变量读取（仅此一份）----
EMAIL_USER    = os.getenv("EMAIL_USER", "")
EMAIL_PASS    = os.getenv("EMAIL_PASS", "")
EMAIL_TO      = os.getenv("EMAIL_TO", "")
SMTP_SERVER   = os.getenv("SMTP_SERVER", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "465")) if os.getenv("SMTP_PORT") else 465
USE_SSL       = SMTP_PORT in (465,)
SENDER_NAME   = os.getenv("SENDER_NAME", "📢 河南招标监控")
WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://zfcg.henan.gov.cn/henan",
}
DB_FILE = "henan_zfcg_v2.db"   # 固定名，配合 GitHub Artifact 持久化
TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_template.html")
# ===============================================

conn = sqlite3.connect(DB_FILE)
conn.execute("""CREATE TABLE IF NOT EXISTS sent (
    hash TEXT PRIMARY KEY, title TEXT, pub_date TEXT, sent_time TEXT)""")


def is_sent(h):
    return conn.execute("SELECT 1 FROM sent WHERE hash=?", (h,)).fetchone() is not None


def mark_sent(h, title, pub):
    conn.execute("INSERT OR IGNORE INTO sent VALUES (?,?,?,?)",
                 (h, title, pub, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


def _parse_items(soup, channel_url):
    """从 BeautifulSoup 里解析出公告列表（抽出来便于重试逻辑复用）"""
    results = []
    lis = soup.select("div.List2 ul li")
    print(f"    div.List2 ul li 命中: {len(lis)} 个")
    for li in lis:
        a = li.select_one("a")
        if not a:
            continue
        title = (a.get("title") or a.get_text(strip=True)).strip()
        href = a.get("href", "").strip()
        if not title or len(title) < 5:
            continue
        time_span = li.select_one("span.Gray.Right")
        pub = time_span.get_text(strip=True) if time_span else ""
        if href and not href.startswith("http"):
            href = "https://zfcg.henan.gov.cn" + href
        h = hashlib.md5((title + href).encode()).hexdigest()
        results.append({"hash": h, "title": title, "link": href, "pub": pub})
    return results


def fetch_channel(channel):
    """★ 带重试的抓取：请求失败自动重试 FETCH_RETRIES 次，间隔 FETCH_DELAY 秒"""
    print(f"  📡 {channel['name']}")
    last_err = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            r = requests.get(channel["url"], headers=HEADERS, timeout=20)
            r.encoding = "UTF-8"
            if r.status_code != 200:
                raise Exception(f"HTTP {r.status_code}")
            soup = BeautifulSoup(r.text, "html.parser")
            return _parse_items(soup, channel["url"])
        except Exception as e:
            last_err = e
            print(f"    ⚠️ 第 {attempt}/{FETCH_RETRIES} 次请求失败: {e}")
            if attempt < FETCH_RETRIES:
                print(f"    🔄 {FETCH_DELAY}s 后重试...")
                time.sleep(FETCH_DELAY)
    print(f"    ❌ 抓取失败（已重试 {FETCH_RETRIES} 次）: {last_err}")
    return []


def in_time_window(pub, start, today):
    if not pub:
        return True
    try:
        fmt = "%Y-%m-%d %H:%M" if len(pub) > 10 else "%Y-%m-%d"
        pd = datetime.strptime(pub, fmt)
        return start <= pd <= today
    except ValueError:
        return True


def apply_filter(items, today, start):
    out = []
    for it in items:
        if not in_time_window(it["pub"], start, today):
            continue
        hit_kw = any(kw in it["title"] for kw in KEYWORDS)
        hit_tp = any(tp in it["title"] for tp in TYPE_ALLOW)
        hit_ex = any(ew in it["title"] for ew in EXCLUDE_WORDS)
        if hit_kw and hit_tp and not hit_ex:
            out.append(it)
    return out


def render_email(items):
    """借鉴 NodeMail：用模板渲染 HTML 邮件"""
    if Template is None:
        print("    ⚠️ jinja2 未安装，使用纯文本降级（请检查 requirements.txt 是否含 jinja2）")
        lines = "\n".join(f"{i+1}. [{it['pub']}] {it['title']}\n    {it['link']}" for i, it in enumerate(items))
        return f"河南政府采购网 新公告 {len(items)} 条\n\n{lines}", "plain"

    with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
        tpl = Template(f.read())
    html = tpl.render(
        items=items,
        keywords="、".join(KEYWORDS),
        types="、".join(TYPE_ALLOW),
        check_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        days_back=DAYS_BACK,
    )
    return html, "html"


def send_email(subject, html, mime):
    """自定义发件人名称：formataddr((名称, 邮箱)) —— 对应 NodeMail 的 from: '"昵称"<邮箱>' """
    if not EMAIL_USER:
        print("    ⚠️ 未配置 EMAIL_USER，跳过邮件")
        return
    receivers = [x.strip() for x in EMAIL_TO.split(",") if x.strip()]
    if not receivers:
        print("    ⚠️ 未配置 EMAIL_TO，跳过邮件")
        return

    msg = MIMEText(html, mime, "utf-8")
    msg["From"] = formataddr((SENDER_NAME, EMAIL_USER))
    msg["To"] = ", ".join(receivers)
    msg["Subject"] = subject

    try:
        if USE_SSL:
            with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=15) as s:
                s.login(EMAIL_USER, EMAIL_PASS)
                s.sendmail(EMAIL_USER, receivers, msg.as_string())
        else:
            with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.login(EMAIL_USER, EMAIL_PASS)
                s.sendmail(EMAIL_USER, receivers, msg.as_string())
        print(f"    ✅ 邮件已发送（{mime}）→ {', '.join(receivers)}")
    except Exception as e:
        print(f"    ❌ 邮件发送失败: {e}")


def push_wecom(items):
    if not WECOM_WEBHOOK:
        return
    lines = "\n".join(
        f"> {i+1}. [{it['pub']}] {it['title']}\n>     [查看]({it['link']})"
        for i, it in enumerate(items)
    )
    content = (f"**📢 河南政府采购网 新公告 x{len(items)}**\n"
               f"> 关键词：{KEYWORDS} | 类型：{TYPE_ALLOW}\n"
               f"> 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
               f"{lines}")
    try:
        r = requests.post(WECOM_WEBHOOK, json={"msgtype": "markdown", "markdown": {"content": content}}, timeout=10)
        if r.json().get("errcode") == 0:
            print(f"    ✅ 企业微信推送成功（{len(items)} 条）")
    except Exception as e:
        print(f"    ❌ 企业微信推送异常: {e}")


def check_once():
    print(f"\n{'='*55}")
    print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 检查 (v{VERSION})")
    today = datetime.now()
    start = today - timedelta(days=DAYS_BACK)
    print(f"   时间窗: {start.strftime('%Y-%m-%d')} ~ {today.strftime('%Y-%m-%d')}")

    all_new = []
    for ch in TARGET_CHANNELS:
        for it in apply_filter(fetch_channel(ch), today, start):
            if is_sent(it["hash"]):
                continue
            all_new.append(it)
            mark_sent(it["hash"], it["title"], it["pub"])

    if all_new:
        print(f"\n  🎯 本次新增 {len(all_new)} 条：")
        for it in all_new:
            print(f"     - [{it['pub']}] {it['title'][:55]}")

        html, mime = render_email(all_new)
        send_email(f"【河南招标监控】{len(all_new)} 条新公告", html, mime)
        push_wecom(all_new)
    else:
        print("  😴 本次无新增公告")
    conn.commit()


def main():
    print("=" * 55)
    print(f"  河南政府采购网 监控 v{VERSION}")
    print(f"  关键词: {KEYWORDS} | 类型: {TYPE_ALLOW}")
    print(f"  时间窗: 近 {DAYS_BACK} 天")
    print(f"  抓取重试: {FETCH_RETRIES} 次（间隔 {FETCH_DELAY}s）")
    print(f"  发件人: {SENDER_NAME} <{EMAIL_USER}>")
    print("=" * 55)
    check_once()


if __name__ == "__main__":
    main()
