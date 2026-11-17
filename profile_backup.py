"""Portable backups for a complete profile library, including avatar bytes."""
from __future__ import annotations

import base64
import binascii
import json
import re
from io import BytesIO

from PIL import Image

from project import PROFILE_FIELDS, TIERS, make_profile


BACKUP_FORMAT = "fuxueli-weibo-profiles"
BACKUP_FILENAME = "fuxueli_profiles.json"
MAX_BACKUP_MB = 32
MAX_AVATAR_BYTES = 4 * 1024 * 1024
PROFILE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def export_profiles(profiles):
    accounts = []
    for pid, profile in profiles.items():
        fields = {key: profile[key] for key in PROFILE_FIELDS}
        avatar = profile["avatar"]
        accounts.append({
            "id": pid,
            **fields,
            "avatar": base64.b64encode(avatar).decode("ascii") if avatar is not None else None,
        })
    return json.dumps(
        {"format": BACKUP_FORMAT, "version": 1, "profiles": accounts},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def import_profiles(store, raw, *, overwrite=False):
    if len(raw) > MAX_BACKUP_MB * 1024 * 1024:
        raise ValueError(f"人物备份文件最大 {MAX_BACKUP_MB} MB。")
    profiles = load_profiles(raw)
    inserted = store.save_many(profiles, overwrite=overwrite)
    return inserted, len(profiles) - inserted


def _decode_avatar(encoded):
    if encoded is None:
        return None
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_AVATAR_BYTES + 2) // 3):
        raise ValueError("头像文件过大或格式有误。")
    try:
        avatar = base64.b64decode(encoded, validate=True)
        if len(avatar) > MAX_AVATAR_BYTES:
            raise ValueError("头像文件过大。")
        with Image.open(BytesIO(avatar)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"} or max(image.size) > 4096:
                raise ValueError("头像格式或尺寸有误。")
            image.load()
    except (binascii.Error, OSError, Image.DecompressionBombError):
        raise ValueError("头像内容无法读取。") from None
    return avatar


def _decode_account(entry):
    if not isinstance(entry, dict):
        raise ValueError("人物条目格式有误。")
    pid = entry.get("id")
    if not isinstance(pid, str) or not PROFILE_ID.fullmatch(pid):
        raise ValueError("人物编号有误。")
    name = entry.get("name")
    region = entry.get("region", "")
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 24 or "\0" in name
            or not isinstance(region, str) or len(region) > 14 or "\0" in region):
        raise ValueError("昵称或来源地格式有误。")
    try:
        name.encode("utf-8")
        region.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("昵称或来源地包含无法保存的字符。") from None
    tier, level = entry.get("tier"), entry.get("level")
    if tier not in TIERS or type(level) is not int or not 1 <= level <= 20:
        raise ValueError("粉丝类型或等级有误。")
    crop = {}
    for key, default, low, high in (
        ("avatar_zoom", 1.0, 1.0, 4.0), ("avatar_x", .5, 0.0, 1.0), ("avatar_y", .5, 0.0, 1.0),
    ):
        value = entry.get(key, default)
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError("头像裁剪参数有误。")
        crop[key] = float(value)
    return pid, make_profile(name.strip(), tier, level, region.strip(), _decode_avatar(entry.get("avatar")), **crop)


def load_profiles(raw):
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("无法读取 JSON 人物备份。") from None
    if (not isinstance(document, dict) or document.get("format") != BACKUP_FORMAT
            or type(document.get("version")) is not int or document["version"] != 1
            or not isinstance(document.get("profiles"), list)):
        raise ValueError("请选择由小梨微博模拟器导出的人物备份。")
    profiles = {}
    for index, entry in enumerate(document["profiles"], 1):
        try:
            pid, profile = _decode_account(entry)
            if pid in profiles:
                raise ValueError("人物编号重复。")
        except ValueError as error:
            raise ValueError(f"第 {index} 个人物：{error}") from None
        profiles[pid] = profile
    return profiles
