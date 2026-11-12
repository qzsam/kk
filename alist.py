# coding=utf-8
# -*- coding: utf-8 -*-
# ==========================================================
# Alist 影视库 · 狼壳（TVBox 分支）PY 源  v1
# ----------------------------------------------------------
# 站点：http://101.33.210.61:5245
#   标准 Alist + strm 方案，匿名可读（guest 权限即可）。
#
# 目录结构（实测 2026-11-10）：
#   /
#   └── 网盘Q群：488641614
#       ├── strm/                       ← 用这个（每个影片一个目录 + 一个 .strm）
#       │   ├── s磁盘2/{电影,剧集,动漫,综艺,短剧,音乐}
#       │   ├── s磁盘3/{电影,剧集,动漫,综艺}
#       │   ├── s磁盘4/{电影,剧集}
#       │   └── s磁盘5/{电影,电视剧,动漫,综艺,纪录片,...}
#       └── 源文件/                      ← 原始文件，不用
#
# 影片目录名格式：`2012 (2009) {tmdb-8656}` / `万米危机 (2026) {tmdb-1424435}`
#   → 括号里是年份，{tmdb-xxx} 是 TMDB ID，可用来取海报
#
# .strm 文件内容：Alist 自己的 /d/ 直链，带 sign 签名
#   http://101.33.210.61:5245/d/<path>?sign=xxx
#   实测直链裸请求即可（206 + video/x-matroska），不需要 Referer / UA
#
# 已实现：
#   · homeContent     首页 = 各盘的最新电影（按目录名排序取前 N）
#   · categoryContent 分类 = 盘 × 类型，支持翻页
#   · searchContent   搜索 = 遍历所有 strm 目录做名字匹配（带本地缓存）
#   · detailContent   详情 = 扫影片目录里的 .strm，多版本列多条线路
#   · playerContent   播放 = 读 .strm 内容取出直链，透传给播放器
#   · 海报            fs/get 的 thumb 字段（云端图），取不到则留空
#
# 注意：
#   1. Alist 的 /d/ 直链带 sign，sign 可能会轮换。本源每次播放都重新读 .strm，
#      不用缓存，避免 sign 过期。
#   2. 搜索要在所有盘里翻目录，第一次较慢，结果缓存 10 分钟。
# ==========================================================
import json
import re
import time
import urllib.parse
import urllib.request
from urllib.parse import urljoin

try:
    import requests
except Exception:
    requests = None

try:
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider(object):
        def init(self, extend=""):
            pass

        def getName(self):
            return ""

        def isVideoFormat(self, url):
            return False

        def manualVideoCheck(self):
            return False

        def destroy(self):
            pass

        def localProxy(self, param):
            return None


BASE = "http://101.33.210.61:5245"
UA = "Mozilla/5.0 (Linux; Android 12) okhttp/3.12.1"
ROOT = "网盘Q群：488641614"
STRM = "strm"

# TMDB 元数据（用于补简介/演员/海报，非必需；请求失败不影响主流程）
TMDB_API = "http://api.tmdb.org"
TMDB_IMG = "http://images.tmdb.org/t/p"
TMDB_KEY = "7507cde1025c02b8cf583894e7b30cc4"
TMDB_LANG = "zh-CN"

# 要扫描的盘与类型（实测存在的组合）
# 磁盘5/电影 是 A-Z 字母分桶结构，4156 条，整类翻页太慢，
# 因此拆成两个聚合分类 + 各桶明细：
#   "5|电影"      → 全量（跨桶，慢，仅供参考）
#   "5#A|电影" 等 → 单个桶，每桶 5-198 条，翻页快
DISKS = [
    ("s磁盘2", ["电影", "剧集", "动漫", "综艺", "短剧"]),
    ("s磁盘3", ["电影", "剧集", "动漫", "综艺"]),
    ("s磁盘4", ["电影", "剧集"]),
    ("s磁盘5", ["电影", "电视剧", "动漫", "综艺", "纪录片"]),
]

# 磁盘5/电影 的字母桶（顺序即分类顺序）
D5_BUCKETS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ["数字开头", "补充电影"]

# 磁盘5/电视剧 的二级分类（下面还有 A-Z 桶）
D5_TV_GROUPS = ["国产", "日韩剧集", "欧美剧集", "其他剧集"]

# 磁盘5/动漫 的二级分类
D5_ANIME_GROUPS = ["国产动漫", "日韩动漫", "日韩动漫多版本", "欧美动漫", "动态漫"]

# 分类表。type_id 编码规则：
#   "盘序号|类型"            普通（整类一次扫）
#   "盘序号#桶|类型"         磁盘5/电影，按字母桶拆开
#   "盘序号#组|类型"         磁盘5/电视剧、动漫，按二级分类拆开
CATS = []
CAT_GROUP = {}      # type_id -> 子分类名（用于筛选面板）
for _di, (_disk, _types) in enumerate(DISKS):
    for _t in _types:
        if _disk == "s磁盘5" and _t == "电影":
            # 字母桶拆分类，避免 139 页的大列表
            for _b in D5_BUCKETS:
                _tid = "%d#%s|%s" % (_di, _b, _t)
                CATS.append((_tid, "磁盘5·电影 %s" % _b))
            continue
        if _disk == "s磁盘5" and _t == "电视剧":
            for _g in D5_TV_GROUPS:
                _tid = "%d#%s|%s" % (_di, _g, _t)
                CATS.append((_tid, "磁盘5·电视剧 %s" % _g))
                CAT_GROUP[_tid] = _g
            continue
        if _disk == "s磁盘5" and _t == "动漫":
            for _g in D5_ANIME_GROUPS:
                _tid = "%d#%s|%s" % (_di, _g, _t)
                CATS.append((_tid, "磁盘5·动漫 %s" % _g))
                CAT_GROUP[_tid] = _g
            continue
        CATS.append(("%d|%s" % (_di, _t), "%s·%s" % (_disk, _t)))

VIDEO_EXT = (".strm", ".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".rmvb", ".flv", ".wmv", ".mov")

# 播放器只认这几种视频四字符码（沿用山楂4K 的实测结论）
VIDEO_FOURCC = (b"hvc1", b"hev1", b"avc1", b"avc3", b"hvc2", b"hev2")

_PROMO = [
    re.compile(r"(tg|telegram)\s*[:：]?\s*@[\w\.\-]+", re.I),
    re.compile(r"(?<![a-zA-Z])(qq|微信|vx|v信)\s*群?\s*[:：]?\s*[0-9]{5,}", re.I),
    re.compile(r"公众号\s*[:：]?\s*[^\s，,。；;]{1,20}"),
    re.compile(r"网盘\s*Q\s*群\s*[:：]?\s*\d+", re.I),
]


def _clean(text):
    s = re.sub(r"<[^>]+>", "", str(text or ""))
    for p in _PROMO:
        s = p.sub("", s)
    return re.sub(r"\s+", " ", s.replace("\u3000", " ")).strip()


def _parse_name(dirname):
    """从影片目录名/文件名解析出 (片名, 年份, tmdb_id)

    实测支持两种 TMDB 标注写法：
      '2012 (2009) {tmdb-8656}'                        → ('2012', '2009', '8656')
      'BLACKPINK 演唱会.2023.[tmdbid-1180359]'          → ('BLACKPINK 演唱会', '2023', '1180359')
      '光猪六壮士 (1997) 1080p.REMUX.{tmdbid-9427}'     → ('光猪六壮士', '1997', '9427')

    文件名里常带分辨率/编码/音轨等噪声，会一并剥掉。
    策略：先按分隔符切段，逐段判断是不是噪声，是就丢，剩下的拼回片名。
    """
    s = str(dirname or "").strip()
    # 1) 去视频扩展名
    s = re.sub(r"\.(strm|mkv|mp4|ts|m2ts|avi|rmvb|flv|wmv|mov)$", "", s, flags=re.I)

    # 2) 提取 TMDB ID（tmdb-xxx / tmdbid-xxx，方括号或花括号）
    tmdb = ""
    m = re.search(r"[\[\{]\s*tmdb(?:id)?[-_]\s*(\d+)\s*[\]\}]", s, re.I)
    if m:
        tmdb = m.group(1)
        s = s[:m.start()] + " " + s[m.end():]

    # 3) 提取年份（优先括号包裹，其次被分隔符包围的独立年份）
    year = ""
    m = re.search(r"[\(\（\[]\s*((?:19|20)\d{2})\s*[\)\）\]]", s)
    if m:
        year = m.group(1)
        s = s[:m.start()] + " " + s[m.end():]
    else:
        m = re.search(r"(?:^|[\.\_\s\-])((?:19|20)\d{2})(?=$|[\.\_\s\-])", s)
        if m:
            year = m.group(1)
            s = s[:m.start(1)] + " " + s[m.end(1):]

    # 4) 把 DTS-HD / DTS-X / ATSC 这类连字符噪声先整体抹掉，避免被切段拆开
    s = re.sub(r"\b(?:dts[-\.]?hd(?:[-\.]?ma)?|dts[-\.]?x|true[-\.]?hd|e?-?ac-?3|"
               r"web[-\.]?dl|blu[-\.]?ray|h\.?26[45])\b", " ", s, flags=re.I)

    # 5) 按分隔符切段（. _ - 和空白），逐段过滤噪声
    parts = re.split(r"[\.\_\s]+", s)
    NOISE = re.compile(
        r"^(?:2160p|1080p|1080i|720p|576p|480p|4k|uhd|"
        r"remux|bluray|webrip|webdl|hdtv|dvdrip|bdrip|"
        r"h26[45]|x26[45]|hevc|avc|avc1|10bit|8bit|hdr|hdr10|dv|dolby|"
        r"aac\d*|ac3|eac3|ddp|dd|dts|hd|ma|atmos|truehd|flac|pcm|lpcm|"
        r"proper|uncut|repack|internal|multi|dual|2audio|3audio|"
        r"chs|cht|eng|jpn|kor|srt|ass|中字|简中|繁中|国语|粤语|英语|双语|中英|无字|"
        r"complete|limited|extended|remastered|mteam|beast|frds|wiki|eur|usa|jpn|"
        r"disc\d+|cd\d+|part\d+|"
        r"x26[45]-[\w.]+|h26[45]-[\w.]+|[\w.]+-[\w]+|"
        r"tmdb(?:id)?-\d+|"
        r"mp3|mp2|opus|wav|"
        r"\d{3,4}x\d{3,4}|"
        r"\d+audio|\d+\.\d+|\d+bit|\d+p|\d+kbps)$", re.I)
    keep = []
    for p in parts:
        t = p.strip(" ()（）[]{}【】-—")
        if not t:
            continue
        if NOISE.match(t):
            continue
        # 纯数字段：2 位以内是 5.1 拆出来的；4 位是码率(3000/8000)。
        # 但 19xx/20xx 可能是片名的一部分（《2012》《1917》），保留。
        if t.isdigit():
            if len(t) <= 2:
                continue
            if len(t) == 4 and t[:2] in ("19", "20"):
                keep.append(t)
                continue
            if 3 <= len(t) <= 6:
                continue
        keep.append(t)
    name = " ".join(keep).strip(" .-_[]{}【】")
    # 全角括号是片名的一部分（蝙蝠侠（系列）），只在两侧配对时才保留
    if name.count("（") != name.count("）"):
        name = name.replace("（", "").replace("）", "")
    if name.count("(") != name.count(")"):
        name = name.replace("(", "").replace(")", "")

    # 5) 全英文且原本用点分隔的，保留空格形式（A.Man.and.A.Woman → A Man and A Woman）
    name = re.sub(r"\s{2,}", " ", name).strip()

    # 6) 兜底：如果全被过滤空了，用去掉扩展名的原名
    if not name:
        name = re.sub(r"\.[^.]+$", "", str(dirname or "")).strip()

    return name, year, tmdb


def _b64u_enc(raw):
    import base64
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_dec(seg):
    import base64
    t = str(seg or "").replace("-", "+").replace("_", "/")
    while len(t) % 4:
        t += "="
    return base64.b64decode(t)


def _size_str(n):
    try:
        n = int(n or 0)
    except Exception:
        return ""
    if n >= 1 << 30:
        return "%.1fG" % (n / float(1 << 30))
    if n >= 1 << 20:
        return "%.0fM" % (n / float(1 << 20))
    return ""


def _natural_key(s):
    """自然排序：S01E02 排在 S01E10 前面"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s or ""))]


def _norm_title(s):
    """片名归一化：去空格/标点/大小写，便于跨语言宽松比对"""
    s = str(s or "").lower()
    s = re.sub(r"[\s\.\-_'’:：,，。！!？\?\(\)（）\[\]【】]+", "", s)
    return s


def _title_match(local, remote):
    """判断本地片名与 TMDB 片名是否指同一部片（宽松）"""
    a, b = _norm_title(local), _norm_title(remote)
    if not a or not b:
        return True
    if a == b or a in b or b in a:
        return True
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= min(3, len(a), len(b))


class Spider(BaseSpider):
    CONFIG = {
        "base": BASE,
        "root": ROOT,
        "timeout": 25,
        "user_agent": UA,
        "page_size": 30,
        "home_limit": 40,       # 首页最多出多少条
        "search_ttl": 600,      # 搜索目录缓存秒数
        "tmdb_enable": 1,       # 1 = 用 TMDB 补简介/演员/海报；0 = 纯本地
        "tmdb_credits": 1,      # 1 = 额外查演职员（多一个请求）；0 = 跳过
        "tmdb_key": TMDB_KEY,
        "tmdb_api": TMDB_API,
        "tmdb_img": TMDB_IMG,
        "tmdb_lang": TMDB_LANG,
        "filter_enable": 1,     # 1 = 分类页显示年份/画质筛选
        "poster_limit": 30,     # 单次列表最多预热多少个海报
        "poster_workers": 12,   # 并发线程数
        "poster_budget": 6,     # 预热总预算（秒），超时不再等剩余请求
        "scan_ttl": 600,        # 目录扫描结果缓存秒数
        "home_types": None,     # 首页计入时间排序的分类；None = 电影/剧集/电视剧/动漫
        "latest_ttl": 900,      # 首页最新列表缓存秒数（全库扫描较慢）
        "latest_buckets": 2,    # 磁盘5 参与计时排序的桶数量
        "api_tries": 3,         # Alist API 重试次数
        "check_timeout": 6,     # 播放前校验单次超时（秒）
        "skip_check": 0,        # 1 = 跳过播放前校验（调试用）
    }

    def __init__(self):
        super().__init__()
        self.cfg = dict(self.CONFIG)
        self.base = str(self.CONFIG["base"]).rstrip("/")
        self._s = None
        self._ls_cache = {}      # 目录 → (时间戳, items)
        self._idx_cache = None   # 全量索引 (时间戳, [影片])
        self._tmdb_cache = {}    # TMDB 结果缓存（含 None，避免反复失败重试）
        self._latest_cache = None  # 首页最新列表缓存 (时间戳, [条目])
        self._scan_cache = {}      # 扫描结果缓存 {路径: (时间戳, [条目])}
        self._detail = {}        # 详情缓存

    # ================= 壳接口 =================
    def getName(self):
        return "Alist影视库"

    def init(self, extend=""):
        self.cfg = dict(self.CONFIG)
        if isinstance(extend, dict):
            self.cfg.update(extend)
        elif isinstance(extend, str) and extend.strip():
            try:
                self.cfg.update(json.loads(extend))
            except Exception:
                for kv in str(extend).split(","):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        self.cfg[k.strip()] = v.strip()
        self.base = str(self.cfg.get("base") or BASE).rstrip("/")
        if requests is not None:
            self._s = requests.Session()
            self._s.headers.update({"User-Agent": self.cfg.get("user_agent") or UA})

    def isVideoFormat(self, url):
        u = str(url or "").lower().split("?")[0]
        return any(u.endswith(x) for x in (".m3u8", ".mp4", ".flv", ".mkv", ".ts"))

    def manualVideoCheck(self):
        return False

    def getDependence(self):
        return ["requests"]

    def isVip(self):
        return False

    def getHeaders(self):
        return {"User-Agent": self.cfg.get("user_agent") or UA}

    def destroy(self):
        try:
            if self._s:
                self._s.close()
        except Exception:
            pass

    def localProxy(self, param):
        return [404, "text/plain", "", None]

    # ================= HTTP 底层（沿用山楂4K 的结构）=================
    @staticmethod
    def _errmsg(e):
        """把底层异常翻译成人能看懂的中文，避免直接抛英文栈信息"""
        m = str(getattr(e, "message", "") or e or "")
        if re.search(r"UnknownHost|Name or service|nodename|getaddrinfo", m, re.I):
            return "域名解析失败，检查网络"
        if re.search(r"timed out|timeout|Timeout", m, re.I):
            return "接口超时，稍后重试"
        if re.search(r"Connection refused|ECONNREFUSED", m, re.I):
            return "服务器拒绝连接"
        if re.search(r"storage not found", m, re.I):
            return "该存储不存在（Alist 侧存储被移除）"
        if re.search(r"object not found|not a folder", m, re.I):
            return "路径不存在"
        if re.search(r"401|unauthorized|token", m, re.I):
            return "未授权，可能需要登录"
        if re.search(r"403|forbidden", m, re.I):
            return "无权限访问"
        if re.search(r"404", m, re.I):
            return "资源不存在"
        return m[:80] or "未知错误"

    def _raw_request(self, url, method="GET", body=None, headers=None, timeout=None,
                     allow_redirects=True):
        """统一底层请求，返回 (status, content_bytes, headers_dict, final_url)。

        requests 可用走 Session，否则退回 urllib。两端行为保持一致，
        上层不用关心依赖是否存在。
        """
        t = timeout or self.cfg.get("timeout", 25)
        hh = dict(headers or {})
        try:
            if self._s is not None:
                if method.upper() == "POST":
                    r = self._s.post(url, data=body, headers=hh, timeout=t,
                                     allow_redirects=allow_redirects)
                else:
                    r = self._s.get(url, headers=hh, timeout=t,
                                    allow_redirects=allow_redirects)
                return r.status_code, (r.content or b""), dict(r.headers), str(r.url)
            req = urllib.request.Request(url, data=body, headers=hh, method=method.upper())
            resp = urllib.request.urlopen(req, timeout=t)
            return resp.getcode(), resp.read(), dict(resp.headers), url
        except urllib.error.HTTPError as e:
            return e.code, (e.read() or b""), dict(e.headers or {}), url
        except Exception as e:
            raise RuntimeError(self._errmsg(e))

    def _post(self, path, body, tries=None):
        """调 Alist API，返回 data 字段或 None。tries 提到 3（山楂4K 同款）。"""
        if tries is None:
            tries = int(self.cfg.get("api_tries") or 3)
        url = self.base + path
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last = ""
        for i in range(max(1, tries)):
            try:
                status, content, _h, _u = self._raw_request(
                    url, "POST", raw, {"Content-Type": "application/json"})
                if status >= 400:
                    last = self._errmsg("HTTP %d" % status)
                    # 401/403/404 不重试
                    if status in (401, 403, 404):
                        break
                    if i < tries - 1:
                        time.sleep(1.0 * (i + 1))
                    continue
                try:
                    d = json.loads((content or b"").decode("utf-8", "replace"))
                except Exception:
                    last = "响应非 JSON"
                    if i < tries - 1:
                        time.sleep(1.0 * (i + 1))
                    continue
                if isinstance(d, dict) and d.get("code") in (200, 0):
                    return d.get("data") or {}
                last = self._errmsg(str((d or {}).get("message") or "接口返回异常"))
                # 业务级错误（路径不存在/存储失效）重试无意义
                if re.search(r"不存在|无法|无权限|not found|not a folder|removed",
                             last, re.I):
                    break
                if i < tries - 1:
                    time.sleep(1.0 * (i + 1))
            except Exception as e:
                last = self._errmsg(e)
                if i < tries - 1:
                    time.sleep(1.0 * (i + 1))
        print("[al] %s 失败: %s" % (path, last))
        return None

    def _ls(self, path, per_page=200):
        """列目录，带内存缓存（同一次会话内）"""
        now = time.time()
        ck = path
        if ck in self._ls_cache:
            ts, items = self._ls_cache[ck]
            if now - ts < 300:
                return items
        d = self._post("/api/fs/list", {
            "path": path, "password": "", "page": 1,
            "per_page": per_page, "refresh": False,
        })
        items = (d or {}).get("content") or []
        self._ls_cache[ck] = (now, items)
        return items

    def _get_file(self, path):
        d = self._post("/api/fs/get", {"path": path, "password": ""})
        return d or {}

    def _strm_url(self, strm_path):
        """读 .strm 内容，取出里面写的播放地址（可能是本地 /d/ 也可能是外链）"""
        d = self._get_file(strm_path)
        raw = str((d or {}).get("raw_url") or "")
        if not raw:
            return ""
        if raw.startswith("/"):
            raw = self.base + raw
        try:
            status, content, _h, _u = self._raw_request(raw, headers={
                "User-Agent": self.cfg.get("user_agent") or UA})
            if status >= 400:
                print("[al] 读 strm HTTP %d: %s" % (status, strm_path))
                return ""
            txt = (content or b"").decode("utf-8", "replace")
        except Exception as e:
            print("[al] 读 strm 失败 %s: %s" % (strm_path, self._errmsg(e)))
            return ""
        # strm 里就是一整行 URL
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("http://") or line.startswith("https://"):
                return line
        return ""

    # ================= 目录索引 =================
    def _disk_path(self, disk, typ):
        return "/%s/%s/%s/%s" % (self.cfg.get("root") or ROOT, STRM, disk, typ)

    def _scan(self, disk, typ, only_dirs=True, bucket=""):
        """扫一个 盘/类型 目录，返回影片条目列表。

        各盘结构不统一，必须逐层识别（depth 上限 4）：
          A) 片名目录/片名.strm               磁盘2/3/4 电影
          B) 片名目录/Season 01/xx.strm       剧集
          C) A-Z 桶/片名.strm                 磁盘5/电影
          D) A-Z 桶/系列合集/xx.strm          磁盘5 里的“蝙蝠侠（系列）”
          E) 分类/A-Z 桶/片名                磁盘5/电视剧（三层！）
          F) 分类/片名/Season 01/xx.strm      磁盘5/动漫（两层）
          G) 片名/(分集文件)                  磁盘5/综艺、纪录片

        识别规则：桶目录和分类目录都只是“容器”，只有含视频文件的目录
        或者本身就该被当作影片的目录才产出条目。
        """
        p = self._disk_path(disk, typ)
        root = p.rstrip("/") + ("/" + bucket if bucket else "")

        # 扫描结果整体缓存。目录树变动不频繁，缓存 10 分钟足够，
        # 期间翻页/切筛选都不用重新扫。
        now = time.time()
        ttl = int(self.cfg.get("scan_ttl") or 600)
        ck = root
        if ck in self._scan_cache:
            ts, cached = self._scan_cache[ck]
            if now - ts < ttl:
                return cached

        out = []
        self._walk(root, disk, typ, out, depth=0, maxd=4)
        # 去重
        uniq, seen = [], set()
        for e in out:
            if e["path"] in seen:
                continue
            seen.add(e["path"])
            uniq.append(e)
        self._scan_cache[ck] = (now, uniq)
        return uniq

    # 只作容器、本身不是影片的分类目录名
    GROUP_NAMES = ("其他剧集", "国产", "日韩剧集", "欧美剧集", "动态漫",
                   "国产动漫", "日韩动漫", "日韩动漫多版本", "欧美动漫",
                   "电视剧", "电影", "动漫", "综艺", "纪录片", "短剧",
                   "补充电影", "数字开头")

    # 已知存在「分类/桶/片名」三层的入口（磁盘5/电视剧）
    DEEP_GROUPS = ("其他剧集", "国产", "日韩剧集", "欧美剧集")

    def _walk(self, path, disk, typ, out, depth=0, maxd=4):
        """递归展开目录树，把“影片”收集到 out 里。

        性能关键：不对每个片名目录都发 _ls（那样 130 部要 60 秒）。
        规则是——
          · 桶目录 / 分类目录：必须展开（这是结构性的，数量少）
          · 其它目录：直接当一部影片，不再探测内部
        只有已知的「分类/桶/片名」三层入口才多钻一层。
        详情页反正会自己 _ls 拿分集，这里不需要提前确认。
        """
        if depth > maxd:
            return
        try:
            items = self._ls(path)
        except Exception as e:
            print("[al] walk %s: %s" % (path[-30:], str(e)[:40]))
            return
        if not items:
            return

        for it in items:
            nm = str(it.get("name") or "")
            if not nm or nm.startswith("."):
                continue
            is_dir = bool(it.get("is_dir"))
            child = path.rstrip("/") + "/" + nm

            if not is_dir:
                if self._is_video(nm):
                    out.append(self._mk_entry(path, nm, disk, typ, False, it.get("modified")))
                continue

            # 桶 / 分类目录 → 必须往下钻
            if self._is_bucket(nm) or nm in self.GROUP_NAMES:
                self._walk(child, disk, typ, out, depth + 1, maxd)
                continue

            # 磁盘5/电视剧 的「国产/日韩剧集」下面是 A-Z 桶而不是片名
            # 这类已在 GROUP_NAMES 里处理；其余目录一律当影片
            out.append(self._mk_entry(path, nm, disk, typ, True, it.get("modified")))

    @staticmethod
    def _is_video(name):
        return any(str(name).lower().endswith(x) for x in VIDEO_EXT)

    @staticmethod
    def _is_bucket(name):
        """判断是不是 A-Z / 数字开头 这类首字母分桶目录"""
        s = str(name or "").strip()
        if s in ("数字开头", "补充电影", "其它", "其他", "未分类"):
            return True
        return bool(re.match(r"^[A-Za-z]$", s))

    def _mk_entry(self, parent, name, disk, typ, is_dir=True, modified=""):
        """构造一条影片条目。文件型条目的片名从文件名解析。"""
        title, year, tmdb = _parse_name(name)
        return {
            "dir": name,
            "path": parent.rstrip("/") + "/" + name,
            "is_dir": is_dir,
            "title": title or name,
            "year": year,
            "tmdb": tmdb,
            "disk": disk,
            "type": typ,
            "size": 0,
            "modified": str(modified or ""),
            "quality": self._quality_of(name),
        }

    def _all_index(self):
        """全量索引（搜索用），缓存 search_ttl 秒"""
        now = time.time()
        if self._idx_cache and now - self._idx_cache[0] < int(self.cfg.get("search_ttl") or 600):
            return self._idx_cache[1]
        allv = []
        for disk, types in DISKS:
            for typ in types:
                try:
                    for v in self._scan(disk, typ):
                        allv.append(v)
                except Exception as e:
                    print("[al] 扫 %s/%s 失败: %s" % (disk, typ, str(e)[:50]))
        self._idx_cache = (now, allv)
        return allv

    # ================= 条目转换 =================
    def _vid(self, e):
        # 把路径编码成 vod_id：base64url(路径)，避免特殊字符
        return _b64u_enc(str(e.get("path") or "").encode("utf-8"))

    def _path_of(self, vid):
        try:
            p = _b64u_dec(vid).decode("utf-8", "ignore")
        except Exception:
            return ""
        # 只接受指向本站根下的绝对路径，挡住 base64 解出的垃圾
        if not p.startswith("/") or len(p) < 3:
            return ""
        return p

    def _kind_of(self, e):
        """判断条目该查 movie 还是 tv"""
        t = str(e.get("type") or "")
        if t in ("剧集", "电视剧", "动漫", "综艺", "短剧", "纪录片"):
            return "tv"
        if t == "电影":
            return "movie"
        p = str(e.get("path") or "")
        return "tv" if ("Season" in p or "S01" in p) else "movie"

    # ================= 筛选条件 =================
    # 只有能从目录名/文件名可靠提取的字段才做筛选项，避免出现大量空结果：
    #   年份  —— 目录名里的 (2025)，覆盖率最高
    #   画质  —— 文件名里的 2160p/1080p/720p，磁盘5 才有
    #   首字母 —— 桶名，磁盘5 的电影/电视剧有
    QUALITY_PAT = re.compile(r"(2160p|1080p|1080i|720p|4k|uhd)", re.I)

    def _quality_of(self, name):
        m = self.QUALITY_PAT.search(str(name or ""))
        if not m:
            return ""
        q = m.group(1).lower()
        if q in ("2160p", "4k", "uhd"):
            return "4K"
        if q in ("1080p", "1080i"):
            return "1080P"
        if q == "720p":
            return "720P"
        return ""

    def _filters_for(self, tid):
        """按分类生成筛选面板。壳的 filters 结构：
           {"分类type_id": [{"key":"year","name":"年份","value":[{"v":"2025","n":"2025"}]}]}
        """
        if not self.cfg.get("filter_enable", 1):
            return {}
        # 年份：最近 12 年 + 更早
        years = [{"v": str(y), "n": str(y)} for y in range(int(time.strftime("%Y")) + 1,
                                                           int(time.strftime("%Y")) - 12, -1)]
        years.append({"v": "older", "n": "更早"})
        groups = [
            {"key": "year", "name": "年份", "value": years},
            {"key": "quality", "name": "画质", "value": [
                {"v": "4K", "n": "4K"}, {"v": "1080P", "n": "1080P"},
                {"v": "720P", "n": "720P"}]},
        ]
        return {str(tid): groups}

    def _apply_filters(self, entries, extend):
        """按 extend 里的 year/quality 过滤条目"""
        if not extend or not isinstance(extend, dict):
            return entries
        y = str(extend.get("year") or "").strip()
        q = str(extend.get("quality") or "").strip().upper()
        out = entries
        if y:
            if y == "older":
                cut = int(time.strftime("%Y")) - 12
                out = [e for e in out
                       if e.get("year") and e["year"].isdigit() and int(e["year"]) < cut]
            elif y.isdigit():
                out = [e for e in out if e.get("year") == y]
        if q:
            out = [e for e in out if e.get("quality") == q]
        return out

    def _item(self, e, thumb=""):
        remarks = e.get("year") or ""
        if e.get("type"):
            remarks = (e["type"] + (" · " + remarks if remarks else ""))
        # 封面：优先用调用方给的，其次查 TMDB 缓存（首页会先 _warm_posters 预热）
        if not thumb and e.get("tmdb"):
            kind = self._kind_of(e)
            ck = "%s:%s:%s" % (kind, e["tmdb"], self.cfg.get("tmdb_lang") or TMDB_LANG)
            hit = self._tmdb_cache.get(ck)
            if hit and hit[1]:
                thumb = hit[1].get("poster") or hit[1].get("fanart") or ""
        return {
            "vod_id": self._vid(e),
            "vod_name": e.get("title") or e.get("dir") or "",
            "vod_pic": thumb,
            "vod_remarks": remarks,
            "vod_year": e.get("year") or "",
            "vod_area": e.get("disk") or "",
            "vod_class": e.get("type") or "",
            "vod_actor": "",
            "vod_director": "",
            "vod_content": "",
            "type_id": e.get("type") or "",
        }

    def _warm_posters(self, entries):
        """并发预取 TMDB 海报，供首页/分类列表显示封面。

        串行太慢（单次 0.4-1s，40 条要 40 秒），这里用线程池压到
        2-4 秒。失败静默跳过，不影响列表。
        """
        if not self.cfg.get("tmdb_enable", 1) or not entries:
            return
        todo = []
        for e in entries:
            tid = e.get("tmdb")
            if not tid:
                continue
            kind = self._kind_of(e)
            ck = "%s:%s:%s" % (kind, tid, self.cfg.get("tmdb_lang") or TMDB_LANG)
            if ck in self._tmdb_cache:
                continue
            todo.append((tid, kind))
        if not todo:
            return

        # 单次预热上限：一页最多 30 条，这里再截一道，
        # 避免翻到 193 条的大分类时一次发上百个请求（实测要 8 秒）
        cap = int(self.cfg.get("poster_limit") or 30)
        todo = todo[:cap]

        workers = int(self.cfg.get("poster_workers") or 12)
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import threading
            deadline = time.time() + float(self.cfg.get("poster_budget") or 6)
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                futs = {ex.submit(self._tmdb, t, k): (t, k) for t, k in todo}
                for fu in as_completed(futs, timeout=None):
                    # 超预算就不再等剩下的，已拿到的照样用
                    if time.time() > deadline:
                        for f in futs:
                            f.cancel()
                        break
                    try:
                        fu.result()
                    except Exception:
                        pass
        except Exception:
            # 线程池不可用就退回串行，最多取前 12 个
            for tid, kind in todo[:12]:
                self._tmdb(tid, kind)

    # ================= 首页 =================
    def homeContent(self, filter=False):
        classes = [{"type_id": tid, "type_name": nm} for tid, nm in CATS]
        filters = {}
        if filter and self.cfg.get("filter_enable", 1):
            for tid, _nm in CATS:
                filters.update(self._filters_for(tid))
        result = {"class": classes, "filters": filters, "list": []}
        limit = int(self.cfg.get("home_limit") or 40)

        # 首页 = 全库按 modified 倒序的最新 N 部（各盘各类型合并排序）
        picked = self._latest(limit)
        # 并发预热 TMDB 海报（2-4 秒），拿不到就留空，不影响列表
        self._warm_posters(picked)
        result["list"] = [self._item(e) for e in picked]
        return result

    def _latest(self, limit):
        """跨盘跨类型取最新的 limit 部影片。

        站点不给全局时间线，只能扫各分类目录再按 modified 排序。
        为了不每次都全扫（磁盘5 有 4156 条），这里只扫「主要」分类：
        各盘的 电影/剧集/电视剧/动漫/综艺，跳过字母分桶（它们在 _scan 里
        会自动展开，数据量太大，且时间戳都集中在同一批，参考价值低）。
        """
        now = time.time()
        ttl = int(self.cfg.get("latest_ttl") or 900)
        if self._latest_cache and now - self._latest_cache[0] < ttl:
            return [e for e in self._latest_cache[1][:limit]]

        # 只扫主要分类。综艺/短剧/纪录片内容少、更新也慢，
        # 全扫会让首页首次加载从 5 秒涨到 15 秒，不值。
        # 需要更全可以把 home_types 配成 ["电影","剧集","电视剧","动漫","综艺","短剧","纪录片"]
        want = self.cfg.get("home_types") or ["电影", "剧集", "电视剧", "动漫"]
        pool = []
        for disk, types in DISKS:
            for typ in types:
                if typ not in want:
                    continue
                # 磁盘5 的电影是 A-Z 分桶（4156 条），改用下面单独处理
                if disk == "s磁盘5" and typ == "电影":
                    continue
                try:
                    pool.extend(self._scan(disk, typ))
                except Exception as e:
                    print("[al] latest %s/%s: %s" % (disk, typ, str(e)[:40]))

        # 磁盘5 的桶：只挑时间最新的 3 个桶（每桶最多 200 条）
        try:
            p5 = self._disk_path("s磁盘5", "电影")
            buckets = [it for it in self._ls(p5)
                       if it.get("is_dir") and self._is_bucket(it.get("name") or "")]
            buckets.sort(key=lambda x: str(x.get("modified") or ""), reverse=True)
            for b in buckets[:int(self.cfg.get("latest_buckets") or 3)]:
                try:
                    pool.extend(self._scan("s磁盘5", "电影", bucket=b["name"]))
                except Exception:
                    pass
        except Exception as e:
            print("[al] latest 磁盘5桶: %s" % str(e)[:40])

        pool.sort(key=lambda e: str(e.get("modified") or ""), reverse=True)
        self._latest_cache = (now, pool)
        return pool[:limit]

    def homeVideoContent(self):
        return {"list": self.homeContent(False).get("list") or []}

    # ================= 分类 =================
    def categoryContent(self, tid, pg, filter, extend):
        pg = int(pg) if str(pg).isdigit() else 1
        per = int(self.cfg.get("page_size") or 30)
        result = {"list": [], "page": pg, "pagecount": 1, "limit": per, "total": 0}
        tid = str(tid or "")
        if "|" not in tid:
            return result
        head, typ = tid.split("|", 1)
        bucket = ""
        if "#" in head:
            di_s, bucket = head.split("#", 1)
        else:
            di_s = head
        try:
            disk = DISKS[int(di_s)][0]
        except Exception:
            return result
        try:
            entries = self._scan(disk, typ, bucket=bucket)
        except Exception as e:
            print("[al] category %s/%s/%s: %s" % (disk, typ, bucket, str(e)[:50]))
            return result
        entries = self._apply_filters(entries, extend)
        total = len(entries)
        result["total"] = total
        result["pagecount"] = max(1, (total + per - 1) // per)
        start = (pg - 1) * per
        page = entries[start:start + per]
        self._warm_posters(page)
        result["list"] = [self._item(e) for e in page]
        return result

    # ================= 搜索 =================
    def searchContent(self, key, quick, pg="1"):
        pg = int(pg) if str(pg).isdigit() else 1
        result = {"list": [], "page": pg, "pagecount": 1, "total": 0}
        kw = str(key or "").strip().lower()
        if not kw:
            return result
        try:
            entries = self._all_index()
        except Exception as e:
            print("[al] search: %s" % str(e)[:60])
            return result
        hits = []
        seen = set()
        for e in entries:
            hay = (e.get("title") or "") + " " + (e.get("dir") or "")
            if kw in hay.lower():
                k = (e.get("dir"), e.get("disk"), e.get("type"))
                if k in seen:
                    continue
                seen.add(k)
                hits.append(e)
        result["total"] = len(hits)
        result["list"] = [self._item(e) for e in hits[:100]]
        return result

    # ================= TMDB 元数据 =================
    def _tmdb_json(self, path):
        """请求 TMDB 的 JSON。失败返回 None。超时压到 8 秒，避免拖慢详情页。"""
        api = str(self.cfg.get("tmdb_api") or TMDB_API).rstrip("/")
        key = self.cfg.get("tmdb_key") or TMDB_KEY
        lang = self.cfg.get("tmdb_lang") or TMDB_LANG
        if not key:
            return None
        url = "%s%s%sapi_key=%s&language=%s" % (
            api, path, "&" if "?" in path else "?", key, lang)
        try:
            status, content, _h, _u = self._raw_request(
                url, headers={"User-Agent": self.cfg.get("user_agent") or UA,
                              "Accept": "application/json"},
                timeout=min(8, int(self.cfg.get("timeout") or 25)))
            if status >= 400:
                print("[al] tmdb HTTP %d: %s" % (status, path))
                return None
            d = json.loads((content or b"").decode("utf-8", "replace"))
            return d if isinstance(d, dict) else None
        except Exception as e:
            print("[al] tmdb 失败 %s: %s" % (path, self._errmsg(e)))
            return None

    def _tmdb(self, tmdb_id, kind):
        """按 TMDB ID 取元数据。kind: 'movie' 或 'tv'。

        返回 dict（title/overview/genres/vote/poster/...）或 None。
        整条链路最多 8 秒，失败静默返回 None，不影响详情页。
        """
        if not tmdb_id or not self.cfg.get("tmdb_enable", 1):
            return None
        key = self.cfg.get("tmdb_key") or TMDB_KEY
        if not key:
            return None
        ck = ("%s:%s:%s" % (kind, tmdb_id, self.cfg.get("tmdb_lang") or TMDB_LANG))
        now = time.time()
        if ck in self._tmdb_cache:
            ts, val = self._tmdb_cache[ck]
            if now - ts < 86400:
                return val

        d = self._tmdb_json("/3/%s/%s" % (kind, tmdb_id))
        if not d:
            self._tmdb_cache[ck] = (now, None)
            return None

        img = str(self.cfg.get("tmdb_img") or TMDB_IMG).rstrip("/")
        poster = str(d.get("poster_path") or "")
        backdrop = str(d.get("backdrop_path") or "")
        val = {
            "title": str(d.get("title") or d.get("name") or ""),
            "orig_title": str(d.get("original_title") or d.get("original_name") or ""),
            "year": str(d.get("release_date") or d.get("first_air_date") or "")[:4],
            "overview": _clean(d.get("overview") or ""),
            "tagline": _clean(d.get("tagline") or ""),
            "genres": " / ".join([g.get("name") or "" for g in (d.get("genres") or [])]),
            "vote": str(d.get("vote_average") or ""),
            "area": ", ".join([c.get("name") or "" for c in (d.get("production_countries") or [])]) or "",
            "lang": str(d.get("original_language") or ""),
            "poster": (img + "/w500" + poster) if poster else "",
            "fanart": (img + "/w780" + backdrop) if backdrop else "",
            "seasons": d.get("number_of_seasons"),
            "episodes": d.get("number_of_episodes"),
            "status": str(d.get("status") or ""),
        }
        # 片名空时用原名兜底
        if not val["title"]:
            val["title"] = val["orig_title"]
        self._tmdb_cache[ck] = (now, val)
        return val

    def _tmdb_credits(self, tmdb_id, kind, limit=8):
        """取演员/导演。失败返回空串。tmdb_credits=0 时直接跳过。"""
        if not tmdb_id or not self.cfg.get("tmdb_enable", 1):
            return "", ""
        if not self.cfg.get("tmdb_credits", 1):
            return "", ""
        ck = "cr:%s:%s" % (kind, tmdb_id)
        now = time.time()
        if ck in self._tmdb_cache:
            ts, val = self._tmdb_cache[ck]
            if now - ts < 86400:
                return val
        acts, dirs = [], []
        d = self._tmdb_json("/3/%s/%s/credits" % (kind, tmdb_id))
        if d:
            for c in (d.get("cast") or [])[:limit]:
                nm = str(c.get("name") or "")
                ch = str(c.get("character") or "")
                if nm:
                    acts.append(nm + ("饰" + ch if ch else ""))
            for c in (d.get("crew") or []):
                if str(c.get("job") or "").lower() in ("director", "导演"):
                    nm = str(c.get("name") or "")
                    if nm:
                        dirs.append(nm)
        val = (", ".join(acts), ", ".join(dirs))
        self._tmdb_cache[ck] = (now, val)
        return val

    def _guess_kind(self, etype, path):
        """判断该用 movie 还是 tv：分类名或目录结构里有 Season 就用 tv"""
        t = str(etype or "")
        if t in ("剧集", "电视剧", "动漫", "综艺", "短剧", "纪录片"):
            return "tv"
        if t == "电影":
            return "movie"
        p = str(path or "")
        if "Season" in p or "S01" in p:
            return "tv"
        return "movie"

    # ================= 详情 =================
    def _collect_videos(self, path, depth=0, maxd=3):
        """递归收集视频/strm 文件（电影是单层，剧集是 Season XX/ 多层）"""
        out = []
        if depth > maxd:
            return out
        for it in self._ls(path):
            nm = str(it.get("name") or "")
            if not nm or nm.startswith("."):
                continue
            if it.get("is_dir"):
                out.extend(self._collect_videos(path.rstrip("/") + "/" + nm, depth + 1, maxd))
                continue
            if not any(nm.lower().endswith(x) for x in VIDEO_EXT):
                continue
            out.append({
                "name": nm,
                "path": path.rstrip("/") + "/" + nm,
                "size": it.get("size") or 0,
            })
        return out

    @staticmethod
    def _ep_label(fname, idx):
        """把文件名整理成集名：优先取 SxxExx，其次取数字，最后用原名"""
        s = re.sub(r"\.[^.]+$", "", str(fname or ""))
        m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", s)
        if m:
            return "S%sE%s" % (m.group(1), m.group(2))
        m = re.search(r"第\s*(\d{1,4})\s*[集话話期]", s)
        if m:
            return "第%s集" % m.group(1)
        m = re.search(r"(?:^|[^0-9])(\d{1,4})(?:[^0-9]|$)", s)
        if m:
            return "第%s集" % m.group(1)
        return s.strip(" .-_") or ("第%d集" % (idx + 1))

    def detailContent(self, ids):
        if not ids:
            return {"list": []}
        vid = ids[0] if isinstance(ids, (list, tuple)) else ids
        vid = str(vid or "").split(",")[0].strip()
        path = self._path_of(vid)
        if not path:
            print("[al] 详情：vod_id 无效")
            return {"list": []}

        # 文件型条目（磁盘5 的 .strm 直接散在桶里）不是目录，不能 _ls
        is_file = any(path.lower().endswith(x) for x in VIDEO_EXT)
        items = [] if is_file else self._ls(path)
        files = [] if is_file else self._collect_videos(path)
        # 兼容：vod_id 直接指向文件的场景
        if not files:
            base = path.rstrip("/").split("/")[-1]
            if any(base.lower().endswith(x) for x in VIDEO_EXT):
                files = [{"name": base, "path": path, "size": 0}]

        dirname = path.rstrip("/").split("/")[-1]
        title, year, tmdb = _parse_name(dirname)

        # 该片属于哪个分类（用于判断 movie / tv）
        etype = ""
        parts = [x for x in path.split("/") if x]
        for cand in reversed(parts[:-1]):
            if cand in ("电影", "剧集", "电视剧", "动漫", "综艺", "短剧", "纪录片"):
                etype = cand
                break

        # 封面：优先 TMDB 海报，取不到则用 Alist 的 thumb
        pic = ""
        meta, actors, directors = None, "", ""
        if tmdb:
            kind = self._guess_kind(etype, path)
            meta = self._tmdb(tmdb, kind)
            if meta:
                # 片名冲突检测：目录名与 TMDB 名字差异过大时以本地为准，
                # 因为采集/整理环节的 TMDB ID 偶尔会挂错（如 2012 → tmdb-8656 实为《天地大冲撞》）
                remote = _norm_title(meta.get("title") or meta.get("orig_title") or "")
                local = _norm_title(title)
                if remote and local and not _title_match(local, remote):
                    print("[al] 片名不一致：本地「%s」 vs TMDB「%s」，以本地为准" % (
                        title, meta.get("title")))
                    meta["_conflict"] = True
                else:
                    if meta.get("title"):
                        title = meta["title"]
                    if meta.get("year"):
                        year = meta["year"]
                    actors, directors = self._tmdb_credits(tmdb, kind)
                pic = meta.get("poster") or meta.get("fanfare") or meta.get("fanart") or ""
        if not pic:
            for it in items:
                th = str(it.get("thumb") or "")
                if th.startswith("http"):
                    pic = th
                    break

        # 简介：TMDB 概述 + 类型/评分/季集数
        content_parts = []
        if meta:
            if meta.get("tagline"):
                content_parts.append(meta["tagline"])
            if meta.get("overview"):
                content_parts.append(meta["overview"])
            facts = []
            if meta.get("genres"):
                facts.append("类型：" + meta["genres"])
            if meta.get("vote"):
                facts.append("评分：" + meta["vote"])
            if meta.get("seasons"):
                facts.append("共%s季%s集" % (meta["seasons"], meta.get("episodes") or "?"))
            if meta.get("area"):
                facts.append("地区：" + meta["area"])
            if facts:
                content_parts.append("　".join(facts))
            if meta.get("_conflict"):
                content_parts.append(
                    "（注：目录标注的 TMDB ID %s 对应《%s》，与本地片名不符，"
                    "已按本地片名显示，元数据仅供参考）" % (tmdb, meta.get("title") or ""))
        vod_content = "\n".join([x for x in content_parts if x])
        if not vod_content:
            vod_content = ""

        play_from, play_url = [], []
        if files:
            # 所有视频文件归入一条线路，按文件名自然排序
            files.sort(key=lambda f: _natural_key(f["name"]))
            cells = []
            single = len(files) == 1
            for i, f in enumerate(files):
                tok = _b64u_enc(json.dumps({"p": f["path"]}, ensure_ascii=False).encode("utf-8"))
                # 单文件影片（电影）直接用片名做集名，避免把年份解析成集号
                label = (title or dirname) if single else self._ep_label(f["name"], i)
                cells.append("%s$%s" % (label, tok))
            play_from.append("直链")
            play_url.append("#".join(cells))

        # 卡片副标题：年份 · 集数 · 评分
        rm = []
        if year:
            rm.append(year)
        if len(files) > 1:
            rm.append("全%d集" % len(files))
        else:
            rm.append(str(etype or "影片"))
        if meta and meta.get("vote") and str(meta["vote"]) != "0.0":
            rm.append("★" + str(meta["vote"]))

        vod = {
            "vod_id": vid,
            "vod_name": title or dirname,
            "vod_pic": pic,
            "vod_year": year,
            "vod_area": (meta or {}).get("area") or "",
            "vod_class": (meta or {}).get("genres") or (etype or ""),
            "type_name": (meta or {}).get("genres") or (etype or ""),
            "vod_lang": (meta or {}).get("lang") or "",
            "vod_remarks": " · ".join([x for x in rm if x]),
            "vod_actor": actors,
            "vod_director": directors,
            "vod_content": vod_content,
            "vod_play_from": "$$$".join(play_from),
            "vod_play_url": "$$$".join(play_url),
        }
        if tmdb:
            print("[al] 详情 %s：tmdb=%s(%s), %d 个文件, 元数据%s" % (
                vod["vod_name"], tmdb, kind if tmdb else "-", len(files),
                "OK" if meta else "无"))
        else:
            print("[al] 详情 %s：%d 个文件, 无tmdb ID" % (vod["vod_name"], len(files)))
        self._detail[vid] = vod
        return {"list": [vod]}

    # ================= 播放地址校验（沿用山楂4K 的 _check / _fourcc）=================
    def _hdr_for(self, url, rng=None):
        """Alist 的 /d/ 直链带 sign，裸请求即可；这里只补 UA 和 Range"""
        h = {"User-Agent": self.cfg.get("user_agent") or UA}
        if rng:
            h["Range"] = rng
        return h

    def _fourcc(self, url, timeout=6):
        """找第一个视频类四字符码。返回 (code, err)。

        音轨 mp4a 排在视轨前面时不能误判，所以跳过 mp4a 继续找。
        与山楂4K 一致：code 为空 + err 非空 = 拿不到头部，不可信。
        """
        try:
            status, b, _h, _u = self._raw_request(
                url, headers=self._hdr_for(url, "bytes=0-1048575"), timeout=timeout)
        except Exception as e:
            return b"", self._errmsg(e)
        if status >= 400:
            return b"", "HTTP %d" % status
        b = b or b""
        if not b:
            return b"", "头部空响应"
        pos = 0
        while True:
            i = b.find(b"stsd", pos)
            if i < 0 or i + 12 > len(b):
                return b"", ""
            code = b[i + 8:i + 12]
            if code == b"mp4a":
                pos = i + 4
                continue
            if code in VIDEO_FOURCC or code in (b"bvc2", b"bv2C", b"av01", b"vp09", b"encv"):
                return code, ""
            pos = i + 4

    def _check(self, url, timeout=6):
        """返回 (ok, kind, why)。校验链与山楂4K 一致：
           · HLS：播放列表非空、首个分片能取到、不是图片占位
           · MP4/MKV：容器魔数正确、有视频轨、中段支持 Range 206
           · TS/FLV：魔数正确即通过
        """
        u = str(url or "")
        if not re.match(r"^https?://", u, re.I):
            return False, "", "地址无效"
        if self.cfg.get("skip_check"):
            return True, "", ""
        try:
            status, b, headers, final_url = self._raw_request(
                u, headers=self._hdr_for(u, "bytes=0-4095"), timeout=timeout)
        except Exception as e:
            return False, "", self._errmsg(e)
        if status >= 400:
            return False, "", "HTTP %d" % status
        b = b or b""
        if not b:
            return False, "", "空响应"
        txt = b.decode("utf-8", "ignore")

        # ---- HLS ----
        if "#EXTM3U" in txt:
            segs = [x.strip() for x in txt.splitlines()
                    if x.strip() and not x.strip().startswith("#")]
            if not segs:
                return False, "hls", "空播放列表"
            su = urljoin(final_url or u, segs[0])
            if re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", su, re.I):
                return False, "hls", "广告占位源"
            try:
                st, sb, _h2, _u2 = self._raw_request(
                    su, headers=self._hdr_for(su, "bytes=0-2047"), timeout=timeout)
                if st >= 400 or not (sb or b""):
                    return False, "hls", "分片取不到"
            except Exception:
                return False, "hls", "分片超时"
            return True, "hls", ""

        # ---- 容器判定 ----
        # MKV/WebM 是 EBML 头 1A 45 DF A3；MP4 是 ftyp/styp；TS 是 0x47 同步字节
        is_mkv = b[:4] == b"\x1a\x45\xdf\xa3"
        is_mp4 = b[4:8] in (b"ftyp", b"styp")
        is_ts = b[0] == 0x47 and len(b) > 188 and b[188] == 0x47
        is_flv = b[:3] == b"FLV"
        if not (is_mkv or is_mp4 or is_ts or is_flv):
            return False, "", "非视频容器"

        # MKV 的 stsd 在 Segment 深处，头部 4KB 里通常取不到，
        # 所以 MKV 只验容器魔数 + 中段 Range，不做四字符码判定
        if is_mp4:
            code, ferr = self._fourcc(u, timeout)
            if code:
                if code not in VIDEO_FOURCC:
                    return False, "mp4", "四字符码不可播(%s)" % code.decode("latin1", "ignore")
            elif ferr:
                return False, "mp4", "取不到头部信息(%s)" % ferr

        if is_mkv or is_mp4:
            try:
                cl = int(headers.get("Content-Length") or 0)
            except Exception:
                cl = 0
            off = 1000000 if (cl <= 0 or cl > 1200000) else max(0, cl // 2)
            try:
                st2, _b2, _h3, _u3 = self._raw_request(
                    u, headers=self._hdr_for(u, "bytes=%d-%d" % (off, off + 2047)),
                    timeout=timeout)
                if st2 != 206:
                    return False, ("mkv" if is_mkv else "mp4"), "中段不支持 Range(%d)" % st2
            except Exception:
                return False, ("mkv" if is_mkv else "mp4"), "中段请求失败"
            return True, ("mkv" if is_mkv else "mp4"), ""
        return True, "ts", ""

    # ================= 播放 =================
    def playerContent(self, flag, id, vipFlags=None):
        out = {"parse": 0, "playUrl": "", "url": "", "header": {}}
        tok = str(id or "")
        fp = ""
        try:
            pd = json.loads(_b64u_dec(tok).decode("utf-8", "ignore"))
            fp = str(pd.get("p") or "")
        except Exception:
            fp = tok
        if not fp:
            print("[al] 播放失败：没有文件路径")
            return out

        # .strm 要读出里面的真实地址；其它文件直接用 Alist 的 /d/ 直链
        url = ""
        if fp.lower().endswith(".strm"):
            url = self._strm_url(fp)
            if not url:
                print("[al] 播放失败：strm 内容为空 %s" % fp)
                return out
        else:
            d = self._get_file(fp)
            url = str(d.get("raw_url") or "")
            if not url:
                print("[al] 播放失败：取不到直链 %s" % fp)
                return out
            if url.startswith("/"):
                url = self.base + url

        # 播放前校验：容器魔数 / 视频轨 / 中段 Range，全通过才交给播放器
        ok, kind, why = self._check(url, int(self.cfg.get("check_timeout") or 6))
        if not ok:
            print("[al] 播放失败：%s（校验 %s）" % (why, fp[-40:]))
            return out

        out["url"] = url
        out["parse"] = 0
        # Alist /d/ 直链带 sign，裸请求即可（实测 206 + video/x-matroska）
        out["header"] = {"User-Agent": self.cfg.get("user_agent") or UA}
        print("[al] 播放[%s] → %s… (%d 字节)" % (kind, url[:64], len(url)))
        return out


if __name__ == "__main__":
    s = Spider()
    s.init("")
    print("name:", s.getName())
    print("分类数:", len(CATS))

    h = s.homeContent(False)
    print("\n首页:", len(h.get("list", [])), "条")
    for x in h["list"][:5]:
        print("   ", x["vod_name"], "|", x["vod_remarks"])

    print("\n=== 分类取样 ===")
    for tid, nm in CATS[:4]:
        c = s.categoryContent(tid, 1, False, {})
        print("  %-18s %3d条 %d页 | %s" % (
            nm, c["total"], c["pagecount"],
            [x["vod_name"] for x in c["list"][:3]]))

    print("\n=== 搜索「古惑仔」 ===")
    q = s.searchContent("古惑仔", False)
    print("  命中:", q["total"])
    for x in q["list"][:3]:
        print("   ", x["vod_name"])

    # 详情 + 播放
    target = None
    for x in h["list"]:
        if x["vod_name"]:
            target = x
            break
    if target:
        print("\n=== 详情: %s ===" % target["vod_name"])
        d = s.detailContent([target["vod_id"]])
        if d.get("list"):
            dd = d["list"][0]
            print("  线路:", dd["vod_play_from"])
            print("  集数:", len(dd["vod_play_url"].split("#")))
            print("  封面:", "有" if dd["vod_pic"] else "无")
            ep = dd["vod_play_url"].split("#")[0]
            if "$" in ep:
                ep = ep.split("$", 1)[1]
            print("\n=== 播放测试 ===")
            s.playerContent("直链", ep)
