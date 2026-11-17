"""Edit and export fictional Weibo posts in Streamlit."""
from __future__ import annotations

from datetime import date
from functools import partial
from uuid import uuid4

import streamlit as st
from PIL import Image

from profile_backup import (
    BACKUP_FILENAME, MAX_BACKUP_MB, export_profiles, import_profiles,
)
from project import (
    EPISODES, MAX_DELAY, TIERS, ProfileStoreError, delete_thread, make_comment,
    make_profile, new_episode, prepare_avatar, render_config, timestamps,
)
from renderer import png_bytes, render_all, zip_bytes
from session_profiles import SessionProfileStore


IMAGE_TYPES = ["png", "jpg", "jpeg", "webp"]
MIN_POST_DATE = date(1900, 1, 1)
MAX_POST_DATE = date(2199, 12, 31)

st.set_page_config(page_title="小梨微博模拟器", page_icon="🍐", layout="wide")
st.markdown("""
<style>
    .block-container {max-width: 1440px; padding-top: 2rem;}
    [data-testid="stHeadingWithActionElements"] h1 {letter-spacing: -.025em;}
</style>
""", unsafe_allow_html=True)


def select_profile(label, key, profiles, **options):
    return st.selectbox(label, list(profiles), format_func=lambda pid: profiles[pid]["name"],
                        key=key, **options)


def bind(widget, label, model, field, key, **options):
    """A hidden or conditionally rendered widget keeps its value in the episode."""
    if key not in st.session_state:
        st.session_state[key] = model[field]

    def save():
        model[field] = st.session_state[key]

    return widget(label, key=key, on_change=save, **options)


def replace_episode(template):
    st.session_state.episode = new_episode(template)
    st.session_state.episode_key = uuid4().hex
    st.session_state.selected_comment = None
    st.session_state.pop("preview_page", None)


def edit_post(episode, profiles, key):
    first, second = st.columns([2, 1])
    with first:
        bind(partial(select_profile, profiles=profiles), "发布账号", episode, "author", key+"author")
    with second:
        bind(st.text_input, "身份标签", episode, "author_badge", key+"badge", max_chars=8,
             placeholder="选填")
    bind(st.text_area, "微博正文", episode, "body", key+"body", height=178, max_chars=280)
    left, right = st.columns(2)
    with left:
        bind(st.date_input, "发布日期", episode, "date", key+"date",
             min_value=MIN_POST_DATE, max_value=MAX_POST_DATE, format="YYYY/MM/DD")
    with right:
        bind(st.time_input, "发布时间", episode, "time", key+"time", step=60, required=True)
    bind(st.text_input, "发布地点", episode, "post_region", key+"region",
         max_chars=16, placeholder="例如 上海")

    upload_key = key + "photos_" + str(st.session_state.get(key+"photo_revision", 0))

    def save_photos():
        files = st.session_state[upload_key]
        episode["photos"] = [file.getvalue() for file in files[:9]]

    uploads = st.file_uploader("微博配图（最多 9 张）", type=IMAGE_TYPES,
                               accept_multiple_files=True, key=upload_key, on_change=save_photos)
    if len(uploads) > 9:
        st.caption("当前使用前 9 张。")
    if episode["photos"]:
        if st.button(f"清空 {len(episode['photos'])} 张配图", key=key+"clear_photos"):
            episode["photos"] = []
            st.session_state[key+"photo_revision"] = st.session_state.get(key+"photo_revision", 0) + 1
            st.rerun()

    with st.expander("互动数字、话题与手机状态"):
        for row in (
            [("转发数", "reposts"), ("评论总数", "comment_count"), ("点赞数", "likes")],
            [("收藏数", "favorites"), ("热度指数", "heat"), ("大家都在搜", "topic")],
        ):
            for col, (label, field) in zip(st.columns(3), row):
                with col:
                    bind(st.text_input, label, episode, field, key+field,
                         max_chars=14 if field == "topic" else 9)
        automatic = bind(st.toggle, "手机时间自动跟随最后一条互动", episode, "phone_auto", key+"phone_auto")
        if not automatic:
            bind(st.time_input, "手机显示时间", episode, "phone_time", key+"phone_time",
                 step=60, required=True)
        bind(st.slider, "电量（%）", episode, "battery_pct", key+"battery", min_value=0, max_value=100)
        bind(st.checkbox, "显示充电状态", episode, "battery_charging", key+"charging")


def edit_comments(episode, profiles, key):
    comments = episode["comments"]
    fans = [pid for pid in profiles if pid != episode["author"]]
    default_fan = next((pid for pid in fans if profiles[pid]["tier"] != "普通"),
                       fans[0] if fans else episode["author"])
    if st.button("＋ 新增顶层评论", key=key+"root", type="primary"):
        previous = max((item["delay"] for item in comments if item["parent_id"] is None), default=-2)
        item = make_comment(default_fan, min(MAX_DELAY, previous + 3))
        comments.append(item)
        st.session_state.selected_comment = item["id"]
        st.rerun()
    if not comments:
        st.caption("添加评论后，可选择账号、设置时间并继续添加回复。")
        return

    entries = {item["id"]: item for item in comments}
    children = {cid: [] for cid in (None, *entries)}
    for item in comments:
        children[item["parent_id"]].append(item["id"])
    _, times = timestamps(episode)
    pending = [(cid, 0) for cid in reversed(children[None])]
    options, labels = [], {}
    while pending:
        cid, depth = pending.pop()
        item = entries[cid]
        stamp = times[cid].strftime("%m-%d %H:%M")
        name = profiles[item["profile_id"]]["name"]
        snippet = " ".join(item["text"].split())[:24] or "待填写"
        indent = "　" * min(depth, 3) + ("↳ " if depth else "")
        options.append(cid)
        labels[cid] = f"{indent}{name} · {snippet} · {stamp}"
        pending.extend((child, depth + 1) for child in reversed(children[cid]))

    chosen = st.session_state.selected_comment
    if chosen not in entries:
        chosen = options[0]
    select_key = key + "comment_selection"
    if st.session_state.get(select_key) != chosen:
        st.session_state[select_key] = chosen

    def choose():
        st.session_state.selected_comment = st.session_state[select_key]

    cid = st.selectbox("选择要编辑的评论或回复", options, format_func=labels.get,
                       key=select_key, on_change=choose)
    item = entries[cid]
    parent = entries.get(item["parent_id"])
    field_key = key + cid + "_"
    with st.container(border=True):
        if parent:
            parent_name = profiles[parent["profile_id"]]["name"]
            st.caption(f"回复 @{parent_name} · {times[cid]:%Y-%m-%d %H:%M}")
        else:
            st.caption(f"评论时间 {times[cid]:%Y-%m-%d %H:%M}")
        bind(partial(select_profile, profiles=profiles), "发言账号", item, "profile_id", field_key+"profile")
        bind(st.text_area, "回复内容" if parent else "评论内容", item, "text", field_key+"text",
             height=108, max_chars=400, placeholder="输入内容，可直接写 @唐心 或插入 Emoji")
        left, right = st.columns([2, 1])
        with left:
            bind(st.number_input, "在对方发言后几分钟回复" if parent else "发博后几分钟评论",
                 item, "delay", field_key+"delay", min_value=1, max_value=MAX_DELAY, step=1)
        with right:
            bind(st.text_input, "这条点赞数", item, "likes", field_key+"likes", max_chars=9)
        bind(st.text_input, "这条的来源地", item, "region", field_key+"region", max_chars=14,
             placeholder=profiles[item["profile_id"]]["region"] or "选填")
        if parent is None:
            bind(st.checkbox, "标为热评", item, "hot", field_key+"hot")
        actions = st.columns([1, 1])
        with actions[0]:
            if st.button("↳ 回复这条", key=field_key+"reply", width="stretch"):
                default = episode["author"] if item["profile_id"] != episode["author"] else default_fan
                new = make_comment(default, 2, cid)
                comments.append(new)
                st.session_state.selected_comment = new["id"]
                st.rerun()
        with actions[1]:
            confirm = st.checkbox("同时删除这条及其楼中楼", key=field_key+"confirm") if children[cid] else True
            if st.button("删除这条", key=field_key+"delete", disabled=not confirm, width="stretch"):
                delete_thread(episode, cid)
                st.session_state.selected_comment = item["parent_id"]
                st.rerun()

    with st.expander("评论截图顺序"):
        roots = children[None]
        bind(st.multiselect, "评论首图优先展示（最多 5 条）", episode, "featured", key+"featured",
             options=roots, format_func=labels.get, max_selections=5)
        st.caption("按选中的顺序排版。较长的楼中楼会自动续页。")


def edit_people(profiles, store):
    st.caption("游客编辑只在当前浏览器会话有效；请在下方下载人物库备份以便下次导入。")
    edit_profile(profiles, store)
    edit_profile_backups(profiles, store)


def edit_profile(profiles, store):
    entries = {"": {"name": "＋ 新增账号"}, **profiles}
    if "person_to_edit" in st.session_state:
        st.session_state["person_selector"] = st.session_state.pop("person_to_edit")
    pid = select_profile("选择常用人物", "person_selector", entries)
    person = profiles.get(pid, make_profile())
    revision = st.session_state.get("profile_form_revision", 0)
    key = f"profile_{pid}_{revision}_"
    with st.form(key, border=True):
        name = st.text_input("昵称", person["name"], max_chars=24)
        region = st.text_input("默认来源地", person["region"], max_chars=14, placeholder="选填")
        left, right = st.columns(2)
        with left:
            tier = st.selectbox("粉丝类型", TIERS, index=TIERS.index(person["tier"]))
        with right:
            level = st.number_input("等级", min_value=1, max_value=20, value=person["level"])
        avatar_file = st.file_uploader("头像图片", type=IMAGE_TYPES, key=key+"avatar")
        if person["avatar"]:
            st.image(person["avatar"], width=65)
        remove_avatar = st.checkbox("清除已保存头像") if person["avatar"] else False
        with st.expander("头像裁剪"):
            zoom = st.slider("放大", 1.0, 4.0, person["avatar_zoom"], 0.1)
            ax = st.slider("横向焦点", 0.0, 1.0, person["avatar_x"], 0.01)
            ay = st.slider("纵向焦点", 0.0, 1.0, person["avatar_y"], 0.01)
        saved = st.form_submit_button("保存人物", type="primary", width="stretch")
    if not saved:
        return
    if not name.strip() or "\0" in name or "\0" in region:
        st.error("请填写有效的昵称和来源地。")
        return
    try:
        avatar = person["avatar"]
        if avatar_file is not None:
            avatar = prepare_avatar(avatar_file.getvalue())
        elif remove_avatar:
            avatar = None
        saved_id = pid or uuid4().hex
        store.save(saved_id, make_profile(name.strip(), tier, level, region.strip(), avatar,
                                         avatar_zoom=zoom, avatar_x=ax, avatar_y=ay))
    except (OSError, ValueError, ProfileStoreError, Image.DecompressionBombError) as error:
        st.error(f"保存失败：{error}")
        return
    st.session_state.person_to_edit = saved_id
    st.session_state.profile_form_revision = revision + 1
    st.toast("人物已保存到当前会话；需要保留请下载备份")
    st.rerun()


def edit_profile_backups(profiles, store):
    with st.expander("人物库备份与迁移"):
        st.download_button(
            "下载完整人物库备份", partial(export_profiles, profiles),
            file_name=BACKUP_FILENAME, mime="application/json", on_click="ignore", width="stretch",
        )
        with st.form("import_profile_backup"):
            upload = st.file_uploader("选择人物库备份", type=["json"], max_upload_size=MAX_BACKUP_MB)
            overwrite = st.checkbox("覆盖相同编号的人物",
                                    help="迁移改过头像的 Fuxueli、唐心等预置人物时勾选。")
            submitted = st.form_submit_button("导入人物和头像", width="stretch")
        if submitted:
            if upload is None:
                st.error("请先选择人物备份文件。")
                return
            try:
                count, skipped = import_profiles(store, upload.getvalue(), overwrite=overwrite)
            except (ValueError, ProfileStoreError) as error:
                st.error(str(error))
                return
            st.toast(f"已导入 {count} 个账号，保留已有同编号账号 {skipped} 个。")
            st.rerun()


def edit_bot(episode, key):
    bind(st.toggle, "导出时显示 Bot", episode, "show_bot", key+"show_bot")
    bind(st.text_input, "Bot 名称", episode, "bot_name", key+"bot_name", max_chars=15)
    bind(st.text_area, "一句话总结", episode, "bot_intro", key+"bot_intro", max_chars=100, height=95)
    bind(st.text_input, "关键词", episode, "bot_keywords", key+"bot_keywords", max_chars=34)
    rows = episode["bot_rows"]
    for row in rows:
        col, num, delete = st.columns([4, 2, 1], vertical_alignment="bottom")
        row_key = key + row["id"] + "_"
        with col:
            bind(st.text_input, "投票选项", row, "label", row_key+"label", max_chars=15)
        with num:
            bind(st.number_input, "占比 %", row, "percent", row_key+"pct", min_value=0, max_value=100, step=1)
        with delete:
            if st.button("删除", key=row_key+"delete"):
                rows.remove(row)
                st.rerun()
    if st.button("＋ 添加投票项", key=key+"bot_add", disabled=len(rows) >= 6):
        rows.append({"id": uuid4().hex, "label": "", "percent": 0})
        st.rerun()
    if rows and episode["show_bot"]:
        total = sum(row["percent"] for row in rows)
        if total != 100:
            st.caption(f"投票占比当前合计 {total}%。")


@st.cache_data(show_spinner=False, max_entries=4)
def make_images(config, scale=2):
    return render_all(config, scale)


def preview(episode, profiles, section):
    st.subheader("截图预览")
    config = render_config(episode, profiles)
    images = make_images(config)
    labels = ["微博正文", *(f"评论页 {n}" for n in range(1, len(images)))]
    previous_section = st.session_state.get("previous_section")
    if st.session_state.get("preview_page") not in labels:
        st.session_state.preview_page = labels[0]
    if section != previous_section:
        if section == "评论与回复" and len(labels) > 1:
            st.session_state.preview_page = labels[1]
        elif section == "微博正文":
            st.session_state.preview_page = labels[0]
        st.session_state.previous_section = section
    label = st.selectbox("选择截图", labels, key="preview_page", label_visibility="collapsed")
    index = labels.index(label)
    left, right = st.columns(2)
    with left:
        st.download_button(
            "下载当前 PNG", partial(png_bytes, images[index]),
            file_name=f"fuxueli_weibo_{index+1:02}.png", mime="image/png",
            on_click="ignore", width="stretch", key="png_download",
        )
    with right:
        st.download_button(
            "下载全部 ZIP", partial(zip_bytes, images),
            file_name="fuxueli_weibo_screenshots.zip", mime="application/zip",
            on_click="ignore", width="stretch", key="zip_download",
        )
    thumbnail = images[index].resize((710, 1536), Image.Resampling.LANCZOS)
    st.image(thumbnail, width=370, caption="导出尺寸 1420 × 3072")


if "episode" not in st.session_state:
    replace_episode(EPISODES[0])
episode = st.session_state.episode
key = st.session_state.episode_key + "_"

store = SessionProfileStore()
profiles = store.list()

st.title("小梨微博模拟器")
with st.sidebar:
    st.caption("公开体验版 · 独立临时人物库（不连接私人数据库）")
    template = st.selectbox("载入一期", EPISODES)
    if st.button("载入模板", width="stretch", help="载入后会替换当前正在编辑的这一期。"):
        replace_episode(template)
        st.rerun()

editor, screenshot = st.columns([1.4, 1], gap="large")
with editor:
    section = st.radio("编辑内容", ("微博正文", "评论与回复", "常用人物", "Bot"),
                       horizontal=True, label_visibility="collapsed", key="section")
    if section == "微博正文":
        edit_post(episode, profiles, key)
    elif section == "评论与回复":
        edit_comments(episode, profiles, key)
    elif section == "常用人物":
        edit_people(profiles, store)
    else:
        edit_bot(episode, key)
with screenshot:
    try:
        preview(episode, profiles, section)
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        st.error(f"截图生成失败：{error}")
