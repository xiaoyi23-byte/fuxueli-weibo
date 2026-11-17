"""Saved accounts, editable episode presets, and the comment timeline."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps


ASSETS = Path(__file__).resolve().parent / "assets"
TIERS = ("普通", "铁粉", "钻粉", "金粉")
EPISODES = ("住院篇", "看星星篇", "空白微博")
MAX_DELAY = 525_600
PROFILE_FIELDS = ("name", "tier", "level", "region", "avatar_zoom", "avatar_x", "avatar_y")


def make_profile(name="", tier="普通", level=1, region="", avatar=None, **crop):
    return {
        "name": name, "tier": tier, "level": level, "region": region,
        "avatar": avatar, "avatar_zoom": 1.0, "avatar_x": 0.5, "avatar_y": 0.5,
        **crop,
    }


def demo_profiles():
    return {
        "fuxueli": make_profile(
            "Fuxueli", avatar=(ASSETS / "avatar.jpg").read_bytes(),
            avatar_zoom=2.3, avatar_x=0.28, avatar_y=0.27,
        ),
        "tangxin": make_profile("唐心", region="上海"),
        "soda": make_profile("梨子味汽水", "铁粉", 2, "浙江"),
        "likelili": make_profile("今天也喜欢梨梨", "钻粉", 2, "上海"),
        "variety": make_profile("百分百追综人", "铁粉", 1, "广东"),
        "updates": make_profile("梨梨今天营业了吗", "金粉", 3, "江苏"),
        "sleepy": make_profile("一只睡不醒的梨", "钻粉", 1, "四川"),
        "winter": make_profile("冬天想吃梨", "铁粉", 2, "北京"),
        "passing": make_profile("今天路过这里", region="湖北"),
        "album": make_profile("阿梨的旧相册", "铁粉", 3, "广东"),
        "downstairs": make_profile("下楼买梨", region="福建"),
    }


def local_profile_path():
    override = os.getenv("FUXUELI_WEIBO_DATA_DIR")
    local = os.getenv("LOCALAPPDATA") or os.getenv("XDG_DATA_HOME")
    base = Path(local) if local else Path.home() / ".local" / "share"
    folder = Path(override).expanduser() if override else base / "FuxueliWeibo"
    return folder / "profiles.sqlite3"


class ProfileStoreError(RuntimeError):
    """A storage error suitable for display without credentials or SQL details."""


class ProfileStore:
    """Store profile fields and avatar bytes together in SQLite or PostgreSQL."""

    def __init__(self, path=None, *, database_url=None):
        self.is_cloud = bool(database_url)
        self.path = Path(path) if path is not None else local_profile_path()
        if self.is_cloud:
            try:
                import psycopg
            except ImportError:
                raise ProfileStoreError("请先安装 requirements.txt 中的 PostgreSQL 驱动。") from None
            self._connect = lambda: psycopg.connect(
                database_url, connect_timeout=12, sslmode="require",
            )
            self._db_error = psycopg.Error
            self._table = "fuxueli_profiles"
            self._order = "position"
            self._parameters = "%s, %s, %s"
            self._initialize_cloud()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connect = lambda: sqlite3.connect(self.path, timeout=10)
            self._db_error = sqlite3.Error
            self._table = "profiles"
            self._order = "rowid"
            self._parameters = "?, ?, ?"
            self._initialize_local()

    def _initialize_local(self):
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS profiles "
                    "(id TEXT PRIMARY KEY, details TEXT NOT NULL, avatar BLOB)"
                )
                if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                    self._write(db, demo_profiles(), "DO NOTHING")
                    db.execute("PRAGMA user_version = 1")
        except self._db_error:
            raise ProfileStoreError("无法初始化本机人物库，请检查文件夹权限。") from None

    def _initialize_cloud(self):
        try:
            with closing(self._connect()) as db, db:
                if db.execute("SELECT to_regclass('fuxueli_profiles')").fetchone()[0] is None:
                    db.execute("SELECT pg_advisory_xact_lock(3810249173)")
                    if db.execute("SELECT to_regclass('fuxueli_profiles')").fetchone()[0] is None:
                        db.execute(
                            "CREATE TABLE fuxueli_profiles ("
                            "id TEXT PRIMARY KEY, details TEXT NOT NULL, avatar BYTEA, "
                            "position BIGINT GENERATED ALWAYS AS IDENTITY)"
                        )
                        self._write(db, demo_profiles(), "DO NOTHING")
        except self._db_error:
            raise ProfileStoreError("无法连接或初始化云端人物库，请检查连接串和数据库权限。") from None

    @staticmethod
    def record(pid, profile):
        details = {key: profile[key] for key in PROFILE_FIELDS}
        return pid, json.dumps(details, ensure_ascii=False), profile.get("avatar")

    def _write(self, db, profiles, conflict):
        sql = (
            f"INSERT INTO {self._table} (id, details, avatar) VALUES ({self._parameters}) "
            f"ON CONFLICT (id) {conflict}"
        )
        records = [self.record(pid, person) for pid, person in profiles.items()]
        with closing(db.cursor()) as cursor:
            cursor.executemany(sql, records)
            return cursor.rowcount

    def list(self):
        try:
            with closing(self._connect()) as db, db:
                rows = db.execute(
                    f"SELECT id, details, avatar FROM {self._table} ORDER BY {self._order}"
                )
                return {
                    pid: {**json.loads(details), "avatar": bytes(avatar) if avatar is not None else None}
                    for pid, details, avatar in rows
                }
        except (self._db_error, ValueError, TypeError):
            raise ProfileStoreError("读取人物库失败，请重试。") from None

    def save(self, pid, profile):
        self.save_many({pid: profile}, overwrite=True)

    def save_many(self, profiles, *, overwrite=False):
        conflict = (
            "DO UPDATE SET details=excluded.details, avatar=excluded.avatar"
            if overwrite else "DO NOTHING"
        )
        try:
            with closing(self._connect()) as db, db:
                return self._write(db, profiles, conflict)
        except self._db_error:
            raise ProfileStoreError("人物资料未能写入，请检查连接后重试。") from None


def prepare_avatar(raw):
    with Image.open(BytesIO(raw)) as image:
        resized = ImageOps.exif_transpose(image).convert("RGB")
        resized.thumbnail((720, 720), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        resized.save(buffer, format="JPEG", quality=93)
        return buffer.getvalue()


def make_comment(profile_id, delay=1, parent_id=None, **fields):
    return {
        "id": uuid4().hex, "profile_id": profile_id, "parent_id": parent_id,
        "text": "", "likes": "", "region": "", "delay": delay, "hot": False,
        **fields,
    }


def new_episode(template):
    episode = {
        "author": "fuxueli", "author_badge": "演员", "body": "", "photos": [],
        "date": date.today(), "time": time(21, 0), "post_region": "",
        "topic": "", "likes": "", "reposts": "", "comment_count": "", "favorites": "",
        "heat": "", "comments": [], "featured": [],
        "phone_auto": True, "phone_time": time(21, 30),
        "battery_pct": 27, "battery_charging": True,
        "show_bot": False, "bot_name": "小梨星语 Bot", "bot_intro": "",
        "bot_keywords": "", "bot_rows": [],
    }
    if template == "看星星篇":
        fill_stars(episode)
    elif template == "住院篇":
        fill_hospital(episode)
    return episode


def root(episode, profile, delay, text, likes, hot=False):
    item = make_comment(profile, delay, text=text, likes=likes, hot=hot)
    episode["comments"].append(item)
    return item["id"]


def reply(episode, parent, profile, delay, text, likes=""):
    item = make_comment(profile, delay, parent, text=text, likes=likes)
    episode["comments"].append(item)
    return item["id"]


def set_poll(episode, rows):
    episode["bot_rows"] = [
        {"id": uuid4().hex, "label": label, "percent": percent} for label, percent in rows
    ]


def fill_hospital(episode):
    episode.update(
        date=date(2026, 11, 13), time=time(21, 17), post_region="上海",
        body="刚睡醒，手机才充上电。消息都看到了，我没事，今天已经能吃东西了。\n\n"
             "这两天先让我赖会儿。也谢谢昨晚帮我的人，等他好一点，我再去看他。",
        likes="8.6万", reposts="1.4万", comment_count="2.7万", favorites="3962",
        heat="9.1万+", topic="付雪梨发微博报平安",
        bot_intro="大家最想留给梨梨的一句话。",
        bot_keywords="早点好起来",
    )
    root(episode, "likelili", 1, "看到你发微博，我才敢把手机放下。今晚能睡着了。", "4271", True)
    ask_tang = root(
        episode, "soda", 3,
        "@唐心 唐心姐拜托照顾好你家艺人！！她要是说能开工了，你先别信。", "3592", True,
    )
    first_fan = reply(episode, ask_tang, "winter", 2, "工作群也给她静音一下吧。", "604")
    reply(episode, first_fan, "likelili", 3, "我刚还在群里劝人别催自拍。", "287")
    reply(episode, ask_tang, "tangxin", 16, "工作往后挪了，刚喝了半碗粥。", "2108")
    subway = root(
        episode, "sleepy", 6,
        "群里说你更新了，我在地铁上刷了半天才刷出来。能吃饭就好。", "1846",
    )
    reply(episode, subway, "fuxueli", 18, "到家早点睡。我也睡了。", "1336")
    root(episode, "passing", 9, "也希望帮她的人早点好起来。谢谢他。", "1138")
    root(episode, "updates", 13, "支持多赖两天，难得你肯给自己放假。", "916")
    root(episode, "album", 21, "@唐心 有消息你告诉我们一声就好，别特地叫她起来拍照了。", "661")
    root(episode, "downstairs", 31, "本来打了好长一段，想了想还是就一句吧。梨梨，平安就好。", "352")
    episode["featured"] = [
        item["id"] for item in episode["comments"] if item["parent_id"] is None
    ][:5]
    set_poll(episode, [("安心休息", 54), ("两个人都早点好起来", 31), ("等你慢慢回来", 15)])


def fill_stars(episode):
    episode.update(
        date=date(2026, 8, 12), time=time(23, 48), post_region="马来西亚",
        body="看星星，嘻嘻。", photos=[(ASSETS / "stars.png").read_bytes()],
        likes="8.2万", reposts="1.8万", comment_count="3.6万", favorites="3.6万",
        heat="8.7万+", topic="马来西亚的星空",
        show_bot=True,
        bot_intro="一张雨后星空，评论区一半在看星星，一半在猜心事。",
        bot_keywords="热词：星星、想念、拍戏、早点休息",
    )
    episode["featured"] = [
        root(episode, "soda", 14, "啊啊啊宝宝拍的天也太漂亮了吧！", "3528", True),
        root(episode, "likelili", 18, "姐姐这是在想谁呀（捂嘴）", "2414"),
        root(episode, "variety", 24, "救命，我怎么又嗑到了何录和雪梨。", "1803"),
    ]
    root(episode, "updates", 31, "看个星星都能被拉郎啊，美女只是觉得星星好看好吗！", "2106", True)
    root(episode, "sleepy", 40, "最近一直在外面拍戏，注意休息呀宝宝！", "1492")
    set_poll(episode, [("夸赞星空与照片", 44), ("猜测她在思念谁", 32),
                       ("提醒别过度嗑CP", 15), ("叮嘱拍戏注意休息", 9)])


def timestamps(episode):
    """Offsets are relative to the post or the immediate parent comment."""
    published = datetime.combine(episode["date"], episode["time"])
    result = {}
    for item in episode["comments"]:
        base = result[item["parent_id"]] if item["parent_id"] else published
        result[item["id"]] = base + timedelta(minutes=item["delay"])
    return published, result


def delete_thread(episode, target_id):
    removed = {target_id}
    for item in episode["comments"]:
        if item["parent_id"] in removed:
            removed.add(item["id"])
    episode["comments"] = [item for item in episode["comments"] if item["id"] not in removed]
    episode["featured"] = [cid for cid in episode["featured"] if cid not in removed]


def render_config(episode, profiles):
    published, times = timestamps(episode)
    author = profiles[episode["author"]]
    visible = {}
    for item in episode["comments"]:
        parent = visible.get(item["parent_id"])
        if not item["text"].strip() or (item["parent_id"] and parent is None):
            continue
        person = profiles[item["profile_id"]]
        region = item["region"].strip() or person["region"]
        stamp = times[item["id"]]
        visible[item["id"]] = {
            **person, **item,
            "owner": item["profile_id"] == episode["author"],
            "reply_to": parent["name"] if parent else "",
            "time": stamp.strftime("%m-%d %H:%M") + (f" · 来自{region}" if region else ""),
            "timestamp": stamp,
        }
    clock = episode["phone_time"]
    if episode["phone_auto"]:
        latest = max((item["timestamp"] for item in visible.values()), default=published)
        clock = (latest + timedelta(minutes=5)).time()
    post_region = episode["post_region"].strip()
    cfg = {key: val for key, val in episode.items() if key not in {
        "author", "comments", "date", "time", "featured", "phone_auto", "phone_time", "post_region",
    }}
    return {
        **cfg, **author,
        "username": author["name"],
        "phone_time": clock.strftime("%H:%M"),
        "post_time": published.strftime("%m-%d %H:%M")
                     + (f" · 发布于{post_region}" if post_region else ""),
        "comments": list(visible.values()), "featured_comment_ids": episode["featured"],
        "mention_names": tuple(person["name"] for person in profiles.values()),
    }
