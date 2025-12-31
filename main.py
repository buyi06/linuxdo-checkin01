"""
cron: 0 */6 * * *
new Env("Linux.Do 签到")
"""

import os
import random
import time
import functools
import sys
import re
from loguru import logger
from DrissionPage import ChromiumOptions, Chromium
from tabulate import tabulate
from curl_cffi import requests
from bs4 import BeautifulSoup


def _env_bool(name: str, default: str = "false") -> bool:
    """Parse common boolean env representations."""
    return os.environ.get(name, default).strip().lower() in (
        "1",
        "true",
        "on",
        "yes",
        "y",
    )


def _env_int(name: str, default: str) -> int:
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return int(default)


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


def _sleep_jitter(min_s: float, max_s: float) -> None:
    time.sleep(random.uniform(min_s, max_s))


def retry_decorator(retries=3):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == retries - 1:  # 最后一次尝试
                        logger.error(f"函数 {func.__name__} 最终执行失败: {str(e)}")
                    logger.warning(
                        f"函数 {func.__name__} 第 {attempt + 1}/{retries} 次尝试失败: {str(e)}"
                    )
                    time.sleep(1)
            return None

        return wrapper

    return decorator


os.environ.pop("DISPLAY", None)
os.environ.pop("DYLD_LIBRARY_PATH", None)

USERNAME = os.environ.get("LINUXDO_USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD")
BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false",
    "0",
    "off",
]

# ===== 浏览任务参数（可在 GitHub Actions 里通过 env 直接设置） =====
# 一次运行最多浏览多少个帖子
MAX_TOPICS = max(0, _env_int("MAX_TOPICS", "50"))
# 每个帖子滚动次数（越小越快；单帖总耗时由 POST_TARGET_* 兜底补齐）
SCROLL_STEPS = max(0, _env_int("SCROLL_STEPS", "2"))
# 每个帖子之间的等待（秒）。用于降低瞬时负载。
TOPIC_DELAY_MIN = max(0.0, _env_float("TOPIC_DELAY_MIN", "0.4"))
TOPIC_DELAY_MAX = max(TOPIC_DELAY_MIN, _env_float("TOPIC_DELAY_MAX", "1.0"))
# 每次滚动之间的等待（秒）
SCROLL_DELAY_MIN = max(0.0, _env_float("SCROLL_DELAY_MIN", "0.2"))
SCROLL_DELAY_MAX = max(SCROLL_DELAY_MIN, _env_float("SCROLL_DELAY_MAX", "0.7"))
# 连续失败时的退避（秒）
BACKOFF_MIN = max(0.0, _env_float("BACKOFF_MIN", "3.0"))
BACKOFF_MAX = max(BACKOFF_MIN, _env_float("BACKOFF_MAX", "6.0"))
# 干跑模式：只浏览不做写操作（不点赞）
DRY_RUN = _env_bool("DRY_RUN", "false")

# 每帖目标停留时长（秒）：用于把“每帖浏览”节奏固定到一个区间（例如 5~8 秒）。
# 说明：这里包含页面加载、滚动与等待；如果页面加载过慢，实际会高于上限。
POST_TARGET_MIN = max(0.0, _env_float("POST_TARGET_MIN", "5"))
POST_TARGET_MAX = max(POST_TARGET_MIN, _env_float("POST_TARGET_MAX", "8"))
if not USERNAME:
    USERNAME = os.environ.get("USERNAME")
if not PASSWORD:
    PASSWORD = os.environ.get("PASSWORD")
GOTIFY_URL = os.environ.get("GOTIFY_URL")  # Gotify 服务器地址
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN")  # Gotify 应用的 API Token
SC3_PUSH_KEY = os.environ.get("SC3_PUSH_KEY")  # Server酱³ SendKey

HOME_URL = "https://linux.do/"
LOGIN_URL = "https://linux.do/login"
SESSION_URL = "https://linux.do/session"
CSRF_URL = "https://linux.do/session/csrf"
LATEST_JSON_URL = "https://linux.do/latest.json"


class LinuxDoBrowser:
    def __init__(self) -> None:
        from sys import platform

        if platform == "linux" or platform == "linux2":
            platformIdentifier = "X11; Linux x86_64"
        elif platform == "darwin":
            platformIdentifier = "Macintosh; Intel Mac OS X 10_15_7"
        elif platform == "win32":
            platformIdentifier = "Windows NT 10.0; Win64; x64"

        co = (
            ChromiumOptions()
            .headless(True)
            .incognito(True)
            .set_argument("--no-sandbox")
        )
        co.set_user_agent(
            f"Mozilla/5.0 ({platformIdentifier}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        )
        self.browser = Chromium(co)
        self.page = self.browser.new_tab()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )

    def fetch_latest_topic_urls(self, limit: int) -> list:
        """通过 Discourse 的 latest.json 拉取足量帖子链接。

        这样可以突破首页 DOM 上可见主题数量的限制，满足一次运行浏览较多帖的需求。
        """
        if limit <= 0:
            return []

        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": HOME_URL,
        }

        urls: list[str] = []
        seen: set[str] = set()

        # 经验上每页几十条，最多翻 60 页，避免异常死循环
        page = 0
        while len(urls) < limit and page < 60:
            resp = self.session.get(
                f"{LATEST_JSON_URL}?page={page}",
                headers=headers,
                impersonate="chrome136",
            )
            if resp.status_code != 200:
                logger.warning(f"拉取 latest.json 失败: page={page}, status={resp.status_code}")
                break

            try:
                data = resp.json()
            except Exception as e:
                logger.warning(f"latest.json 解析失败: page={page}, err={e}")
                break

            topics = (data.get("topic_list") or {}).get("topics") or []
            if not topics:
                break

            for t in topics:
                tid = t.get("id")
                slug = t.get("slug")
                if not tid or not slug:
                    continue
                url = f"{HOME_URL}t/{slug}/{tid}"
                if url in seen:
                    continue
                seen.add(url)
                urls.append(url)
                if len(urls) >= limit:
                    break

            page += 1

        return urls

    def login(self):
        logger.info("开始登录")
        # Step 1: Get CSRF Token
        logger.info("获取 CSRF token...")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
        }
        resp_csrf = self.session.get(CSRF_URL, headers=headers, impersonate="chrome136")
        csrf_data = resp_csrf.json()
        csrf_token = csrf_data.get("csrf")
        logger.info(f"CSRF Token obtained: {csrf_token[:10]}...")

        # Step 2: Login
        logger.info("正在登录...")
        headers.update(
            {
                "X-CSRF-Token": csrf_token,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://linux.do",
            }
        )

        data = {
            "login": USERNAME,
            "password": PASSWORD,
            "second_factor_method": "1",
            "timezone": "Asia/Shanghai",
        }

        try:
            resp_login = self.session.post(
                SESSION_URL, data=data, impersonate="chrome136", headers=headers
            )

            if resp_login.status_code == 200:
                response_json = resp_login.json()
                if response_json.get("error"):
                    logger.error(f"登录失败: {response_json.get('error')}")
                    return False
                logger.info("登录成功!")
            else:
                logger.error(f"登录失败，状态码: {resp_login.status_code}")
                logger.error(resp_login.text)
                return False
        except Exception as e:
            logger.error(f"登录请求异常: {e}")
            return False

        self.print_connect_info()  # 打印连接信息

        # Step 3: Pass cookies to DrissionPage
        logger.info("同步 Cookie 到 DrissionPage...")

        # Convert requests cookies to DrissionPage format
        # Using standard requests.utils to parse cookiejar if possible, or manual extraction
        # requests.Session().cookies is a specialized object, but might support standard iteration

        # We can iterate over the cookies manually if dict_from_cookiejar doesn't work perfectly
        # or convert to dict first.
        # Assuming requests behaves like requests:

        cookies_dict = self.session.cookies.get_dict()

        dp_cookies = []
        for name, value in cookies_dict.items():
            dp_cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": ".linux.do",
                    "path": "/",
                }
            )

        self.page.set.cookies(dp_cookies)

        logger.info("Cookie 设置完成，导航至 linux.do...")
        self.page.get(HOME_URL)

        time.sleep(5)
        user_ele = self.page.ele("@id=current-user")
        if not user_ele:
            # Fallback check for avatar
            if "avatar" in self.page.html:
                logger.info("登录验证成功 (通过 avatar)")
                return True
            logger.error("登录验证失败 (未找到 current-user)")
            return False
        else:
            logger.info("登录验证成功")
            return True

    def click_topic(self):
        if MAX_TOPICS <= 0:
            logger.info("MAX_TOPICS=0，跳过浏览任务")
            return True

        # 先用 API 拉足量链接，满足一次浏览大量帖的需求；失败则回退到 DOM 抽取。
        urls = []
        try:
            urls = self.fetch_latest_topic_urls(MAX_TOPICS)
        except Exception as e:
            logger.warning(f"拉取帖子链接异常，回退到页面抽取: {e}")

        if not urls:
            topic_list = self.page.ele("@id=list-area").eles(".:title")
            if not topic_list:
                logger.error("未找到主题帖")
                return False
            urls = [t.attr("href") for t in topic_list if t.attr("href")]

        # 去重 + 打散
        dedup = []
        seen = set()
        for u in urls:
            if not u:
                continue
            if u in seen:
                continue
            seen.add(u)
            dedup.append(u)

        random.shuffle(dedup)
        targets = dedup[: min(MAX_TOPICS, len(dedup))]

        logger.info(f"本次计划浏览 {len(targets)} 个帖子")
        for i, url in enumerate(targets, start=1):
            ok = self.click_one_topic(url)
            if not ok:
                _sleep_jitter(BACKOFF_MIN, BACKOFF_MAX)
            _sleep_jitter(TOPIC_DELAY_MIN, TOPIC_DELAY_MAX)
            if i % 25 == 0:
                logger.info(f"已完成 {i}/{len(targets)}")

        return True

    @retry_decorator()
    def click_one_topic(self, topic_url):
        new_page = self.browser.new_tab()
        start_ts = time.time()
        # 为每个帖子随机生成一个目标停留时长；用于保证“每帖 10~15 秒”这类需求。
        target_s = random.uniform(POST_TARGET_MIN, POST_TARGET_MAX)
        try:
            new_page.get(topic_url)
            # 点赞是写操作，允许通过 DRY_RUN 禁用。
            if (not DRY_RUN) and random.random() < 0.3:
                self.click_like(new_page)
            self.browse_post(new_page)
            # 兜底补齐：确保单帖整体耗时达到 target_s
            elapsed = time.time() - start_ts
            remain = target_s - elapsed
            if remain > 0:
                time.sleep(remain)
            return True
        finally:
            try:
                new_page.close()
            except Exception:
                pass

    def browse_post(self, page):
        prev_url = None
        # 开始自动滚动
        for _ in range(SCROLL_STEPS):
            # 随机滚动一段距离
            scroll_distance = random.randint(550, 650)  # 随机滚动 550-650 像素
            logger.info(f"向下滚动 {scroll_distance} 像素...")
            page.run_js(f"window.scrollBy(0, {scroll_distance})")
            logger.info(f"已加载页面: {page.url}")

            # 小概率提前退出，避免每帖行为完全一致
            if random.random() < 0.01:
                logger.success("随机退出浏览")
                break

            # 检查是否到达页面底部
            at_bottom = page.run_js(
                "window.scrollY + window.innerHeight >= document.body.scrollHeight"
            )
            current_url = page.url
            if current_url != prev_url:
                prev_url = current_url
            elif at_bottom and prev_url == current_url:
                logger.success("已到达页面底部，退出浏览")
                break

            # 动态随机等待（可配置）
            wait_time = random.uniform(SCROLL_DELAY_MIN, SCROLL_DELAY_MAX)
            time.sleep(wait_time)

    def run(self):
        login_res = self.login()
        if not login_res:  # 登录
            logger.warning("登录验证失败")

        if BROWSE_ENABLED:
            click_topic_res = self.click_topic()  # 点击主题
            if not click_topic_res:
                logger.error("点击主题失败，程序终止")
                return
            logger.info("完成浏览任务")

        self.send_notifications(BROWSE_ENABLED)  # 发送通知
        self.page.close()
        self.browser.quit()

    def click_like(self, page):
        try:
            if DRY_RUN:
                logger.info("DRY_RUN 启用，跳过点赞")
                return False

            # 尽量只点“未点赞”的按钮（样式/插件差异时仍可能回退到普通选择器）
            like_button = page.ele(
                ".discourse-reactions-reaction-button:not(.reacted)"
            ) or page.ele(".discourse-reactions-reaction-button")
            if like_button:
                logger.info("找到未点赞的帖子，准备点赞")
                like_button.click()
                logger.info("点赞成功")
                _sleep_jitter(0.8, 1.6)
                return True
            else:
                logger.info("帖子可能已经点过赞了")
                return False
        except Exception as e:
            logger.error(f"点赞失败: {str(e)}")
            return False

    def print_connect_info(self):
        logger.info("获取连接信息")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        }
        resp = self.session.get(
            "https://connect.linux.do/", headers=headers, impersonate="chrome136"
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table tr")
        info = []

        for row in rows:
            cells = row.select("td")
            if len(cells) >= 3:
                project = cells[0].text.strip()
                current = cells[1].text.strip() if cells[1].text.strip() else "0"
                requirement = cells[2].text.strip() if cells[2].text.strip() else "0"
                info.append([project, current, requirement])

        print("--------------Connect Info-----------------")
        print(tabulate(info, headers=["项目", "当前", "要求"], tablefmt="pretty"))

    def send_notifications(self, browse_enabled):
        status_msg = "✅每日登录成功"
        if browse_enabled:
            status_msg += " + 浏览任务完成"

        if GOTIFY_URL and GOTIFY_TOKEN:
            try:
                response = requests.post(
                    f"{GOTIFY_URL}/message",
                    params={"token": GOTIFY_TOKEN},
                    json={"title": "LINUX DO", "message": status_msg, "priority": 1},
                    timeout=10,
                )
                response.raise_for_status()
                logger.success("消息已推送至Gotify")
            except Exception as e:
                logger.error(f"Gotify推送失败: {str(e)}")
        else:
            logger.info("未配置Gotify环境变量，跳过通知发送")

        if SC3_PUSH_KEY:
            match = re.match(r"sct(\d+)t", SC3_PUSH_KEY, re.I)
            if not match:
                logger.error(
                    "❌ SC3_PUSH_KEY格式错误，未获取到UID，无法使用Server酱³推送"
                )
                return

            uid = match.group(1)
            url = f"https://{uid}.push.ft07.com/send/{SC3_PUSH_KEY}"
            params = {"title": "LINUX DO", "desp": status_msg}

            attempts = 5
            for attempt in range(attempts):
                try:
                    response = requests.get(url, params=params, timeout=10)
                    response.raise_for_status()
                    logger.success(f"Server酱³推送成功: {response.text}")
                    break
                except Exception as e:
                    logger.error(f"Server酱³推送失败: {str(e)}")
                    if attempt < attempts - 1:
                        sleep_time = random.randint(180, 360)
                        logger.info(f"将在 {sleep_time} 秒后重试...")
                        time.sleep(sleep_time)


if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        print("Please set USERNAME and PASSWORD")
        exit(1)
    l = LinuxDoBrowser()
    l.run()
