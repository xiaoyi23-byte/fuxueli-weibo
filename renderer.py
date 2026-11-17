"""Render editable, fictional Weibo-style screenshots with Pillow.

All positions use a 710 x 1536 logical canvas; output defaults to 2x resolution.
This renderer is deliberately independent of Streamlit so it can be tested and
reused from scripts.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from math import ceil, cos, pi, sin
from pathlib import Path
from typing import Any
from zipfile import ZipFile, ZIP_DEFLATED

from PIL import Image, ImageDraw, ImageFont, ImageOps

from project import demo_profiles, new_episode, render_config


ROOT = Path(__file__).resolve().parent


def choose_font(bold: bool) -> Path:
    """Find a CJK-capable font on Linux, Windows or macOS."""
    options = [
        ROOT / 'assets' / ('NotoSansCJK-Bold.ttc' if bold else 'NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/opentype/noto') / ('NotoSansCJK-Bold.ttc' if bold else 'NotoSansCJK-Regular.ttc'),
        Path('C:/Windows/Fonts/msyhbd.ttc' if bold else 'C:/Windows/Fonts/msyh.ttc'),
        Path('C:/Windows/Fonts/simhei.ttf' if bold else 'C:/Windows/Fonts/simsun.ttc'),
        Path('/System/Library/Fonts/PingFang.ttc'),
        Path('/System/Library/Fonts/STHeiti Medium.ttc'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
    ]
    return next((item for item in options if item.exists()),options[-1])


FONT_REGULAR = choose_font(False)
FONT_BOLD = choose_font(True)
WIDTH, HEIGHT = 710, 1536
EMOJI_FONT_PATH = ROOT / 'assets' / 'NotoColorEmoji.ttf'
MENTION_BLUE = '#507daf'
MENTION_HANDLE = re.compile(r'[\w\-·]{1,30}')
COMMENT_BOTTOM = 1336
LINE_END_MARKS = '，。！？；：、）】》〉」』…,.!?:;)]'


def mention_runs(content: str, names=()):
    """Split text into ordinary and @mention runs, keeping punctuation outside."""
    names = sorted({name for name in names if name}, key=len, reverse=True)
    cursor = 0
    for match in re.finditer(r'(?<![A-Za-z0-9_.+\-])[@＠]', content):
        start, tail = match.start(), match.end()
        if start < cursor:
            continue
        end = tail
        for name in names:
            if content.startswith(name, tail):
                after = tail + len(name)
                next_char = content[after:after+1]
                if name[-1:].isascii() and next_char and re.fullmatch(r'[A-Za-z0-9_]', next_char):
                    continue
                end = after
                break
        if end == tail:
            bare = MENTION_HANDLE.match(content, tail)
            if bare:
                end = bare.end()
        if end > tail:
            if start > cursor:
                yield content[cursor:start], False
            yield content[start:end], True
            cursor = end
    if cursor < len(content):
        yield content[cursor:], False


def has_emoji(text: str) -> bool:
    return any(0x1f000 <= ord(ch) <= 0x1ffff or 0x2600 <= ord(ch) <= 0x27bf or ch == '\u20e3' for ch in text)


def graphemes(text: str) -> list[str]:
    """Group emoji modifiers, variation selectors and ZWJ sequences without extra packages."""
    groups=[]
    for ch in text:
        code=ord(ch)
        attach=(unicodedata.combining(ch)>0 or ch in ('\ufe0e','\ufe0f','\u200d','\u20e3')
                or 0x1f3fb<=code<=0x1f3ff or (groups and groups[-1].endswith('\u200d')))
        if groups and attach:
            groups[-1]+=ch
        elif groups and 0x1f1e6<=code<=0x1f1ff and len(groups[-1])==1 and 0x1f1e6<=ord(groups[-1])<=0x1f1ff:
            groups[-1]+=ch
        else:
            groups.append(ch)
    return groups


def open_image(data: bytes | str | Path | Image.Image | None) -> Image.Image | None:
    if data is None:
        return None
    if isinstance(data, Image.Image):
        return data.convert('RGB')
    try:
        if isinstance(data, bytes):
            data = BytesIO(data)
        with Image.open(data) as source:
            return ImageOps.exif_transpose(source).convert('RGB')
    except (OSError, ValueError):
        return None


@dataclass
class Canvas:
    scale: int = 2

    def __post_init__(self):
        self.image = Image.new('RGB', (WIDTH * self.scale, HEIGHT * self.scale), '#ffffff')
        self.draw = ImageDraw.Draw(self.image)
        self._font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
        self._emoji_cache: dict[tuple[str,int], Image.Image | None] = {}
        self._color_emoji_font = None
        if EMOJI_FONT_PATH.exists():
            try:
                self._color_emoji_font = ImageFont.truetype(str(EMOJI_FONT_PATH),109)
            except OSError:
                pass

    def q(self, x: float) -> int:
        return round(x * self.scale)

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self._font_cache:
            candidate = FONT_BOLD if bold and FONT_BOLD.exists() else FONT_REGULAR
            if not candidate.exists():
                candidate = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
            self._font_cache[key] = ImageFont.truetype(str(candidate), size * self.scale)
        return self._font_cache[key]

    def rect(self, box, color, radius=0, outline=None, width=1):
        b = tuple(self.q(x) for x in box)
        self.draw.rounded_rectangle(
            b, radius=self.q(radius), fill=color,
            outline=outline, width=self.q(width),
        )

    def line(self, xy, fill='#e9e9e9', width=1):
        self.draw.line([(self.q(x), self.q(y)) for x,y in xy], fill=fill, width=self.q(width))

    def circle(self, cx, cy, radius, color, outline=None, width=1):
        r = self.q(radius)
        box = (self.q(cx)-r,self.q(cy)-r,self.q(cx)+r,self.q(cy)+r)
        self.draw.ellipse(box, fill=color, outline=outline, width=self.q(width))

    def _emoji_image(self, cluster: str, size: int) -> Image.Image | None:
        key=(cluster,size)
        if key not in self._emoji_cache:
            if self._color_emoji_font is None:
                self._emoji_cache[key]=None
            else:
                try:
                    layer=Image.new('RGBA',(280,160),(0,0,0,0))
                    ImageDraw.Draw(layer).text((8,0),cluster,font=self._color_emoji_font,embedded_color=True)
                    bbox=layer.getbbox()
                    if bbox:
                        crop=layer.crop(bbox)
                        target_h=max(1,self.q(size)*11//10)
                        target_w=max(1,round(crop.width*target_h/crop.height))
                        self._emoji_cache[key]=crop.resize((target_w,target_h),Image.Resampling.LANCZOS)
                    else:
                        self._emoji_cache[key]=None
                except (OSError,ValueError,UnicodeError):
                    self._emoji_cache[key]=None
        return self._emoji_cache[key]

    def _segments(self, content):
        return graphemes(str(content))

    def text(self, xy, content, size=22, fill='#222222', bold=False, anchor=None):
        value=str(content)
        if not has_emoji(value):
            x,y=map(self.q,xy)
            self.draw.text((x,y),value,font=self.font(size,bold),fill=fill,anchor=anchor,stroke_width=0)
            return
        cursor=self.q(xy[0]); top=self.q(xy[1]); buffer=''
        def draw_buffer():
            nonlocal cursor,buffer
            if buffer:
                self.draw.text((cursor,top),buffer,font=self.font(size,bold),fill=fill,stroke_width=0)
                cursor+=round(self.draw.textlength(buffer,font=self.font(size,bold)))
                buffer=''
        for cluster in self._segments(value):
            if has_emoji(cluster):
                emoji=self._emoji_image(cluster,size)
                if emoji is not None:
                    draw_buffer()
                    self.image.paste(emoji,(cursor,top+self.q(size)//12),emoji)
                    cursor+=emoji.width
                    continue
            buffer+=cluster
        draw_buffer()

    def measure(self, content, size=22, bold=False) -> float:
        value=str(content)
        if has_emoji(value):
            width=0.0; buffer=''
            for cluster in self._segments(value):
                if has_emoji(cluster):
                    emoji=self._emoji_image(cluster,size)
                    if emoji is not None:
                        if buffer:
                            width+=self.draw.textlength(buffer,font=self.font(size,bold));buffer=''
                        width+=emoji.width
                        continue
                buffer+=cluster
            if buffer:
                width+=self.draw.textlength(buffer,font=self.font(size,bold))
            return width/self.scale
        return self.draw.textlength(value, font=self.font(size, bold)) / self.scale

    def wrap(self, content: str, max_width: int, size: int) -> list[str]:
        result=[]
        for paragraph in str(content).split('\n'):
            if not paragraph:
                result.append('')
                continue
            line=''
            for char in self._segments(paragraph):
                if line and self.measure(line+char,size) > max_width:
                    result.append(line)
                    line=char
                else:
                    line+=char
            result.append(line)
        return result or ['']

    def lines(self, content: str, x: int, y: int, max_width: int, size=24,
              step=None, color='#222222', bold=False, max_lines=None) -> int:
        rows=self.wrap(content,max_width,size)
        if max_lines is not None:
            rows=rows[:max_lines]
        step=step or int(size*1.58)
        for line in rows:
            self.text((x,y),line,size,color,bold)
            y+=step
        return y

    def rich_rows(self, content, max_width, size, names=()):
        """Wrap by grapheme and keep mention styling when a line breaks."""
        rows = [[]]
        for run, is_mention in mention_runs(str(content), names):
            for cluster in graphemes(run):
                if cluster == '\n':
                    rows.append([])
                    continue
                current = rows[-1]
                if current and current[-1][1] == is_mention:
                    candidate = current[-1][0] + cluster
                    width = sum(self.measure(text, size) for text, _ in current[:-1])
                    width += self.measure(candidate, size)
                else:
                    width = sum(self.measure(text, size) for text, _ in current)
                    width += self.measure(cluster, size)
                line_limit = max_width + (12 if cluster in LINE_END_MARKS else 0)
                if current and width > line_limit:
                    if cluster in LINE_END_MARKS:
                        previous = graphemes(current[-1][0])
                        last_char = previous.pop()
                        previous_style = current[-1][1]
                        if previous:
                            current[-1] = (''.join(previous), previous_style)
                        else:
                            current.pop()
                        rows.append([(last_char, previous_style), (cluster, is_mention)])
                        continue
                    rows.append([(cluster, is_mention)])
                elif current and current[-1][1] == is_mention:
                    current[-1] = (candidate, is_mention)
                else:
                    current.append((cluster, is_mention))
        return rows

    def draw_rich_rows(self, rows, x, y, size, step, color='#222222'):
        for row in rows:
            cursor = x
            for run, is_mention in row:
                self.text((cursor, y), run, size, MENTION_BLUE if is_mention else color)
                cursor += self.measure(run, size)
            y += step
        return y

    def fit(self, content, max_width, size, bold=False):
        value = str(content)
        if self.measure(value, size, bold) <= max_width:
            return value
        parts = graphemes(value)
        while parts and self.measure(''.join(parts) + '…', size, bold) > max_width:
            parts.pop()
        return ''.join(parts) + '…'

    def image_box(self, source, box, focus=(.5,.5), zoom=1.0, circle=False, radius=8):
        img=open_image(source)
        x,y,w,h=map(self.q,box)
        if img is None:
            self.rect((box[0],box[1],box[0]+box[2],box[1]+box[3]), '#f3f5f4',radius)
            return
        iw,ih=img.size
        crop_w=min(iw,ih*w/h) / max(1.0,zoom)
        crop_h=crop_w*h/w
        cx=float(focus[0])*iw; cy=float(focus[1])*ih
        left=max(0,min(iw-crop_w,cx-crop_w/2))
        top=max(0,min(ih-crop_h,cy-crop_h/2))
        image=img.crop((round(left),round(top),round(left+crop_w),round(top+crop_h)))
        image=image.resize((w,h),Image.Resampling.LANCZOS)
        mask=Image.new('L',(w,h),0)
        m=ImageDraw.Draw(mask)
        if circle:
            m.ellipse((0,0,w-1,h-1),fill=255)
        else:
            m.rounded_rectangle((0,0,w-1,h-1), radius=self.q(radius), fill=255)
        self.image.paste(image,(x,y),mask)

    def gradient_header(self):
        for y in range(self.q(190)):
            a=y/self.q(190)
            col=(round(40-4*a),round(102-31*a),round(127-36*a))
            self.draw.line([(0,y),(self.q(WIDTH),y)],fill=col)

    def heart_outline(self,cx,cy,radius,color='#353535',stroke=3):
        points=[]
        for j in range(65):
            t=j*2*pi/64
            x=16*sin(t)**3
            y=13*cos(t)-5*cos(2*t)-2*cos(3*t)-cos(4*t)
            points.append((cx+x*radius/17,cy-y*radius/17))
        self.line(points,color,stroke)

    def heat_flame(self,cx,cy):
        outer=[(-1,15),(-12,9),(-13,-1),(-7,-11),(-5,-3),(1,-19),(8,-10),(11,-2),(13,5),(8,13)]
        inner=[(-1,12),(-5,7),(-4,1),(0,-6),(5,3),(5,9)]
        self.draw.polygon([(self.q(cx+x),self.q(cy+y)) for x,y in outer],fill='#fb7045')
        self.draw.polygon([(self.q(cx+x),self.q(cy+y)) for x,y in inner],fill='#ffc56b')


def header(c: Canvas, cfg: dict):
    c.gradient_header()
    c.text((43,16),cfg.get('phone_time','14:35'),27,'#ffffff',True)
    # Draw status indicators as shapes, avoiding missing-glyph boxes on Linux.
    for k,bar_h in enumerate((8,13,18,23)):
        c.rect((552+k*10,43-bar_h,558+k*10,43),'#ffffff',1)
    c.circle(616,31,11,'#ffffff')
    c.circle(616,31,6,'#2a637c')
    battery=max(0,min(100,int(cfg.get('battery_pct',27))))
    charging=bool(cfg.get('battery_charging',True))
    battery_color='#6bbd8d' if charging else ('#e9756c' if battery<=20 else '#e8eef0')
    battery_text_color='#ffffff' if charging or battery<=20 else '#31546a'
    c.rect((648,18,693,46),battery_color,9)
    battery_label=str(battery)
    label_x=671-c.measure(battery_label,20,True)/2
    c.text((label_x,18),battery_label,20,battery_text_color,True)
    c.line([(41,85),(23,110),(41,133)],'#ffffff',4)
    person = {**cfg, 'name': cfg.get('username', 'Fuxueli')}
    draw_avatar(c, person, 82, 81, 58)
    c.text((154,84),c.fit(person['name'], 412, 29, True),29,'#ffffff',True)
    c.text((599,84),'◎',33,'#ffffff')
    c.text((645,84),'•••',29,'#ffffff',True)
    c.rect((0,170,710,1536),'#ffffff',22)


def footer(c: Canvas, cfg: dict):
    c.rect((0,1395,710,1536),'#fbfbfb')
    c.line([(0,1395),(710,1395)],'#eaeaea',1)
    c.circle(65,1452,36,'#e5f2eb')
    c.text((46,1424),'梨',35,'#e57085',True)
    c.text((169,1424),'↗',42,'#333333')
    c.text((215,1431),cfg.get('reposts','1.8万'),25,'#333333',True)
    c.rect((350,1427,393,1470),'#ad6fe4',12)
    c.text((357,1424),'★',30,'#ffe788',True)
    c.text((404,1431),cfg.get('favorites','3.6万'),25,'#333333',True)
    c.heart_outline(561,1450,25,'#353535',3)
    c.text((590,1431),cfg.get('likes','8.2万'),25,'#333333',True)
    c.rect((261,1516,449,1523),'#111111',5)


def fake_note(c: Canvas, y=1355):
    c.text((525,y),'同人模拟',16,'#b9b9b9')


def draw_avatar(c, person, x, y, side):
    avatar = person.get('avatar')
    if avatar is not None:
        c.image_box(avatar, (x, y, side, side),
                    (person.get('avatar_x', .5), person.get('avatar_y', .5)),
                    person.get('avatar_zoom', 1.0), True)
    else:
        colors = ['#a87d65', '#9a7693', '#7393a8', '#b97979', '#858cb4', '#679184']
        name = person.get('name') or '梨'
        color = colors[sum(map(ord, name)) % len(colors)]
        c.circle(x + side/2, y + side/2, side/2, color)
        initial = graphemes(name)[0]
        size = round(side * .48)
        c.text((x + (side-c.measure(initial, size, True))/2, y + side*.19),
               initial, size, '#ffffff', True)


def main_post(cfg: dict, scale=2) -> Image.Image:
    c = Canvas(scale)
    header(c, cfg)
    username = cfg.get('username', 'Fuxueli')
    draw_avatar(c, {**cfg, 'name': username}, 32, 226, 80)
    badge = str(cfg.get('author_badge', '演员')).strip()
    badge_width = c.measure(badge, 18) + 16
    name = c.fit(username, 482 - (badge_width + 52 if badge else 0), 30, True)
    c.text((132, 228), name, 30, '#e9792d', True)
    if badge:
        vx = 148 + c.measure(name, 30, True)
        c.circle(vx + 10, 248, 12, '#f3b62b')
        c.text((vx + 4, 234), 'V', 17, '#ffffff', True)
        c.rect((vx + 34, 238, vx + 34 + badge_width, 268), '#fff0ce', 8)
        c.text((vx + 42, 240), badge, 18, '#a37c46')
    stamp = c.fit(cfg.get('post_time', ''), 490, 21)
    c.text((132, 278), stamp, 21, '#999999')
    c.text((640, 231), '☆', 37, '#b7b7b7')

    photos = [photo for photo in cfg.get('photos', []) if photo is not None][:9]
    count = len(photos)
    cols = 2 if count in (2, 4) else 3
    grid_rows = ceil(count / cols)
    heat = str(cfg.get('heat', '')).strip()
    topic = str(cfg.get('topic', '')).strip()
    photo_bottom = 1329 - (74 if heat else 0) - (83 if topic else 0)
    photo_room = 235 if count == 1 else grid_rows*113 + 25 if count else 0
    y = 350
    body = str(cfg.get('body', '')).strip()
    if body:
        body = re.sub(r'\n{3,}', '\n\n', body)
        names = cfg.get('mention_names', ())
        for size in (36, 33, 30, 27, 24, 21):
            step = round(size * 1.45)
            rows = c.rich_rows(body, 650, size, names)
            if y + len(rows)*step + 18 + photo_room <= photo_bottom:
                break
        else:
            rows = c.rich_rows(' '.join(body.split()), 650, size, names)
        y = c.draw_rich_rows(rows, 30, y, size, step) + 18
    if count == 1:
        room = min(700, photo_bottom-y-60)
        c.image_box(photos[0], (29, y, 652, room), (.5, .39), 1, False, 10)
        y += room + 14
        c.text((30, y), '1张图片', 19, '#999999')
        y += 40
    elif count:
        gap = 7
        side = min((652 - (cols-1)*gap) / cols, (photo_bottom-y-22)/grid_rows - gap)
        for index, photo in enumerate(photos):
            row, col = divmod(index, cols)
            c.image_box(photo, (29+col*(side+gap), y+row*(side+gap), side, side),
                        (.5, .5), 1, False, 5)
        y += grid_rows*(side+gap) + 20
    if heat:
        c.rect((28, y, 295, y+52), '#f7f7f7', 26)
        c.heat_flame(58, y+26)
        c.text((80, y+8), c.fit('热度 ' + heat, 197, 23), 23, '#777777')
        y += 74
    if topic:
        c.text((29, y), '大家都在搜', 20, '#aaaaaa')
        c.rect((28, y+35, 345, y+81), '#f5f5f5', 23)
        c.text((47, y+42), c.fit(topic, 280, 20), 20, '#777777')
    fake_note(c, 1340)
    footer(c, cfg)
    return c.image


def comment_tabs(c: Canvas,cfg:dict):
    c.text((30,208),'转发 '+str(cfg.get('reposts','1.8万')),26,'#aaaaaa')
    c.text((270,208),'评论 '+str(cfg.get('comment_count','3.6万')),28,'#222222',True)
    c.text((540,208),'赞 '+str(cfg.get('likes','8.2万')),26,'#aaaaaa')
    c.rect((288,271,416,277),'#b46beb',4)
    c.line([(0,282),(710,282)],'#eeeeee',1)


@dataclass(frozen=True)
class CommentLayout:
    size: int
    step: int
    avatar: int
    x: int
    text_offset: int
    metadata_height: int


TOP_COMMENT = CommentLayout(27, 40, 72, 127, 46, 38)
COMPACT_COMMENT = CommentLayout(23, 32, 59, 116, 34, 29)
REPLY = CommentLayout(21, 29, 36, 186, 31, 29)
BADGE_COLORS = {'铁粉': '#6ca9d1', '钻粉': '#b885d8', '金粉': '#d4a445', '博主': '#ed8e38'}


@dataclass
class CommentBlock:
    item: dict
    layout: CommentLayout
    rows: list

    @property
    def height(self):
        return self.layout.text_offset + self.layout.step*len(self.rows) + self.layout.metadata_height


def comment_block(c, item, layout, names):
    body = str(item.get('text', '')).strip()
    parent_name = item.get('reply_to', '')
    if parent_name:
        body = f'回复 @{parent_name}：{body}'
    rows = c.rich_rows(body, 682-layout.x, layout.size, names)
    return CommentBlock(item, layout, rows)


def draw_comment_block(c, block, y, follow_on=False):
    item, layout = block.item, block.layout
    x = layout.x
    avatar_x = 138 if layout is REPLY else 30
    name_size = layout.size - 1
    if layout is REPLY:
        c.line([(126, y), (126, y+block.height-3)], '#e8edf1', 2)
    draw_avatar(c, item, avatar_x, y, layout.avatar)

    tier = item.get('tier', '普通')
    if item.get('owner'):
        tier, badge = '博主', '博主'
    else:
        badge = f"{tier}{item.get('level', 1)}" if tier in BADGE_COLORS else ''
    label_size = 15
    badge_width = c.measure(badge, label_size) + 14 if badge else 0
    name_width = 592-x-badge_width if item.get('hot') else 661-x-badge_width
    name = c.fit(item.get('name', '粉丝'), name_width, name_size, item.get('owner', False))
    c.text((x, y), name, name_size, '#df732d', item.get('owner', False))
    if badge:
        badge_x = x+c.measure(name, name_size, item.get('owner', False))+8
        c.rect((badge_x, y+4, badge_x+badge_width, y+27), BADGE_COLORS[tier], 5)
        c.text((badge_x+7, y+5), badge, label_size, '#ffffff', True)
    if item.get('hot') and layout is not REPLY:
        c.text((631, y+5), '热评', 18, '#df6d2d', True)
    if follow_on:
        c.text((605 if item.get('hot') else 647, y+4), '续', 16, '#8392a5')

    bottom = c.draw_rich_rows(block.rows, x, y+layout.text_offset, layout.size, layout.step)
    meta_y = bottom + 5
    meta = c.fit(item.get('time', ''), (552 if layout is REPLY else 472)-x, 16)
    c.text((x, meta_y), meta, 16, '#929292')
    if layout is not REPLY:
        c.text((482, meta_y-6), '↗', 24, '#999999')
        c.rect((530, meta_y-2, 550, meta_y+14), None, 4, '#999999', 2)
        c.line([(537, meta_y+14), (534, meta_y+20), (544, meta_y+14)], '#999999', 2)
    likes = c.fit(item.get('likes', ''), 65, 17)
    likes_x = 682-c.measure(likes, 17)
    c.heart_outline(likes_x-17 if likes else 663, meta_y+9, 10, '#e76b2c', 2)
    c.text((likes_x, meta_y-1), likes, 17, '#e76b2c')
    return y + block.height


def ordered_threads(cfg):
    """Order the first screenshot by its selected roots and replies by time."""
    roots, children = [], {}
    for index, source in enumerate(cfg.get('comments', ())):
        item = {**source}
        if not item.get('id'):
            item['id'] = f'legacy-{index}'
        parent = item.get('parent_id')
        if parent:
            children.setdefault(parent, []).append(item)
        else:
            roots.append(item)
        if item.get('owner_reply'):
            children.setdefault(item['id'], []).append({
                'id': f"{item['id']}-owner", 'parent_id': item['id'],
                'owner': True, 'name': cfg.get('username', 'Fuxueli'),
                'avatar': cfg.get('avatar'), 'avatar_zoom': cfg.get('avatar_zoom', 2.3),
                'avatar_x': cfg.get('avatar_x', .28), 'avatar_y': cfg.get('avatar_y', .27),
                'text': item['owner_reply'], 'reply_to': item.get('name', '粉丝'),
                'time': item.get('owner_reply_time', '刚刚'),
                'likes': item.get('owner_reply_likes', ''),
            })
    featured_ids = cfg.get('featured_comment_ids')
    if featured_ids is None:
        featured = roots[:3]
    else:
        lookup = {item['id']: item for item in roots}
        featured = [lookup[cid] for cid in dict.fromkeys(featured_ids[:5]) if cid in lookup]
    featured_set = {item['id'] for item in featured}
    rest = [item for item in roots if item['id'] not in featured_set]
    if not featured:
        featured, rest = rest[:3], rest[3:]

    def thread(root):
        replies = []
        pending = list(reversed(children.get(root['id'], ())))
        while pending:
            item = pending.pop()
            replies.append(item)
            pending.extend(reversed(children.get(item['id'], ())))
        replies.sort(key=lambda item: item.get('timestamp').isoformat() if item.get('timestamp') else '')
        return root, replies

    return list(map(thread, featured)), list(map(thread, rest))


def fit_group(c, threads, names):
    space = COMMENT_BOTTOM - 371
    for layout in (TOP_COMMENT, COMPACT_COMMENT):
        blocks = []
        for item, replies in threads:
            blocks.append(comment_block(c, item, layout, names))
            blocks.extend(comment_block(c, reply, REPLY, names) for reply in replies)
        if sum(block.height for block in blocks) + max(0, len(blocks)-1)*7 <= space:
            break
    return blocks


def paginate_blocks(blocks, featured_ids):
    """Split long threads and individual comments at whole text lines."""
    pages = [[]]
    remaining = COMMENT_BOTTOM - (371 if featured_ids else 325)
    root_count = 0
    for block in blocks:
        is_root = block.layout is not REPLY
        start_next_page = (
            block.item['id'] not in featured_ids if len(pages) == 1 and featured_ids else root_count >= 3
        )
        if is_root and start_next_page and pages[-1]:
            pages.append([])
            remaining = COMMENT_BOTTOM-325
            root_count = 0
        rows = block.rows
        continued = False
        overhead = block.layout.text_offset + block.layout.metadata_height
        while rows:
            max_rows = (remaining-overhead)//block.layout.step
            if max_rows < min(len(rows), 2) and pages[-1]:
                pages.append([])
                remaining = COMMENT_BOTTOM-325
                root_count = 0
                max_rows = (remaining-overhead)//block.layout.step
            part = CommentBlock(block.item, block.layout, rows[:max_rows])
            pages[-1].append((part, continued))
            root_count += int(is_root)
            remaining -= part.height + 7
            rows = rows[max_rows:]
            continued = True
    return pages


def bot_height(cfg):
    return 250 + min(6,len(cfg.get('bot_rows',[])))*58


def draw_bot(c: Canvas,cfg:dict,y:int):
    rows=cfg.get('bot_rows',[])[:6]
    height=bot_height(cfg)
    c.rect((20,y,690,y+height),'#f5faf7',19,'#d5e7de',1)
    c.rect((42,y+24,109,y+91),'#5bae92',19)
    c.text((55,y+37),'梨',36,'#ffffff',True)
    c.text((125,y+25),cfg.get('bot_name','小梨星语 Bot'),27,'#337467',True)
    c.rect((355,y+33,507,y+64),'#e5f2eb',9)
    c.text((365,y+35),'原创模拟助手',17,'#518a7a')
    intro=cfg.get('bot_intro','一张雨后星空，评论区一半在看星星，一半在猜心事。')
    iy=c.lines(intro,42,y+111,615,21,35,'#657f75',max_lines=2)
    bars_y=max(y+184,iy+12)
    for n,row in enumerate(rows):
        pct=max(0,min(100,int(row.get('percent',0))))
        by=bars_y+n*58
        c.rect((43,by,666,by+49),'#fafffb',8,'#c9ded3')
        c.rect((44,by+1,44+max(20,621*pct/100),by+48),'#d3ebe0',7)
        c.text((58,by+9),str(row.get('label',''))[:15],21,'#4c7b6c')
        pct_text=f'{pct}%'
        c.text((651-c.measure(pct_text,22,True),by+8),pct_text,22,'#4c7b6c',True)
    c.text((45,y+height-45),cfg.get('bot_keywords','热词：星星、想念、拍戏、早点休息')[:34],17,'#739b8b')
    return y+height


def comment_pages(cfg: dict, scale=2) -> list[Image.Image]:
    featured, rest = ordered_threads(cfg)
    show_bot = bool(cfg.get('show_bot', True))
    if not featured and not rest and not show_bot:
        return []
    c = Canvas(scale)
    names = (*cfg.get('mention_names', ()), cfg.get('username', ''),
             *(item.get('name', '') for item in cfg.get('comments', ())))
    blocks = fit_group(c, featured, names)
    for item, replies in rest:
        blocks.append(comment_block(c, item, TOP_COMMENT, names))
        blocks.extend(comment_block(c, reply, REPLY, names) for reply in replies)
    pages = paginate_blocks(blocks, {root['id'] for root, _ in featured})
    if show_bot:
        last_top = 371 if len(pages) == 1 and featured else 325
        used = sum(block.height for block, _ in pages[-1]) + max(0, len(pages[-1])-1)*7
        if last_top + used + 18 + bot_height(cfg) > COMMENT_BOTTOM:
            pages.append([])

    images = []
    for index, page in enumerate(pages):
        c = Canvas(scale)
        header(c, cfg)
        comment_tabs(c, cfg)
        top = 325
        if index == 0 and featured:
            c.text((28, 306), '热门评论', 25, '#e67825', True)
            top = 371
        last = index == len(pages)-1
        bot_here = last and show_bot
        bottom = COMMENT_BOTTOM - (bot_height(cfg)+18 if bot_here else 0)
        used = sum(block.height for block, _ in page)
        thread_gaps = sum(block.layout is not REPLY for block, _ in page[1:])
        spare = bottom-top-used-7*max(0, len(page)-1)
        thread_gap = 7 + min(58, spare//thread_gaps) if thread_gaps else 7
        y = top
        for position, (block, continuation) in enumerate(page):
            y = draw_comment_block(c, block, y, continuation)
            if position < len(page)-1:
                new_thread = page[position+1][0].layout is not REPLY
                if new_thread:
                    divider = y + thread_gap//2
                    c.line([(116, divider), (684, divider)], '#ededed', 1)
                y += thread_gap if new_thread else 7
        if bot_here:
            draw_bot(c, cfg, y+18 if page else y)
        elif not last and y+41 <= COMMENT_BOTTOM:
            c.text((130, y+10), '展开更多评论  ›', 22, '#6981a9')
        fake_note(c)
        footer(c, cfg)
        images.append(c.image)
    return images


def render_all(cfg:dict,scale=2)->list[Image.Image]:
    return [main_post(cfg,scale),*comment_pages(cfg,scale)]


def png_bytes(image:Image.Image)->bytes:
    buffer=BytesIO();image.save(buffer,format='PNG',optimize=True);return buffer.getvalue()


def zip_bytes(images:list[Image.Image],prefix='fuxueli_weibo')->bytes:
    buffer=BytesIO()
    with ZipFile(buffer,'w',ZIP_DEFLATED) as z:
        for i,image in enumerate(images,1):
            z.writestr(f'{prefix}_{i:02d}.png',png_bytes(image))
    return buffer.getvalue()


def sample_config() -> dict[str, Any]:
    return render_config(new_episode("看星星篇"), demo_profiles())
