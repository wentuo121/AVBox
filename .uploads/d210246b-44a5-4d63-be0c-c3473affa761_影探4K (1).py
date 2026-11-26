# coding: utf-8
# 影探4K（cms.lyyytv.cn）· TVBox / FongMi Python 站源
# 站型：苹果CMS 的 app 接口（/api.php/app/*）；播放走 fys 解析（parsesId=6 专线 / 5 通用），
#       返回 https://baidu.con/<key16><base64> 形态，用「前 16 字符当 key/iv」AES-CBC 解。
import base64
import json
import re
import time
import urllib.parse
import urllib.request

try:
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider(object):
        pass

HOST = "http://cms.lyyytv.cn"
UA = "okhttp/4.12.0"
# parsesId 实测: 6/7=通用解析(爱奇艺/优酷/腾讯可用), 5=专线(基本失效)
# 通用解析三档回退: 6 → 7 → 5
PARSE_IDS_PAGE = [6, 7, 5]    # 第三方页面解析（爱奇艺等）
PARSE_IDS_MEDIA = [5, 6, 7]   # 直链解析（专线优先，再回退通用）
PARSE_BASE = "http://fys.lyyytv.cn/api/index?parsesId=%d&appid=10001&videoUrl="
CMS_KEY = "wP5bvxoc3yv7FoBQENFZuAF0EUYr4LTy"
DEC_PREFIX = "https://baidu.con/"
YT4K_HOST = re.compile(r"(?:^|\.)(?:ym4kjx|hd4kjx|sd4kjx|mm4kjx|mv4kjx|vk4kjx)\.lyyytv\.cn$", re.I)
MEDIA_RE = re.compile(r"\.(?:mp4|m3u8|m3u|flv|mkv|ts|mov|webm|m4v|mpd)(?:[?#]|$)", re.I)
FILTER_KEYS = [("class", "类型"), ("area", "地区"), ("lang", "语言"), ("year", "年份"),
               ("letter", "字母"), ("by", "排序"), ("sort", "顺序"), ("star", "主演"),
               ("director", "导演"), ("state", "状态"), ("version", "版本")]

# ---------------------------------------------------------------- AES（解密用）
def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _build_sbox():
    inv = [0] * 256
    for i in range(1, 256):
        r, base, e = 1, i, 254
        while e:
            if e & 1:
                r = _gmul(r, base)
            base = _gmul(base, base)
            e >>= 1
        inv[i] = r

    def rotl(x, n):
        return ((x << n) | (x >> (8 - n))) & 0xFF

    return [inv[i] ^ rotl(inv[i], 1) ^ rotl(inv[i], 2) ^ rotl(inv[i], 3) ^ rotl(inv[i], 4) ^ 0x63
            for i in range(256)]


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D]


class _AES(object):
    def __init__(self, key):
        if len(key) not in (16, 24, 32):
            raise ValueError("bad key")
        self.nk = len(key) // 4
        self.nr = self.nk + 6
        self.rk = self._expand(key)

    def _expand(self, key):
        w = [list(key[i:i + 4]) for i in range(0, len(key), 4)]
        for i in range(self.nk, 4 * (self.nr + 1)):
            t = list(w[i - 1])
            if i % self.nk == 0:
                t = t[1:] + t[:1]
                t = [SBOX[b] for b in t]
                t[0] ^= RCON[i // self.nk - 1]
            elif self.nk > 6 and i % self.nk == 4:
                t = [SBOX[b] for b in t]
            w.append([w[i - self.nk][j] ^ t[j] for j in range(4)])
        return w

    def _add(self, s, rnd):
        for c in range(4):
            for r in range(4):
                s[r][c] ^= self.rk[rnd * 4 + c][r]

    def enc(self, block):
        s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
        self._add(s, 0)
        for rnd in range(1, self.nr + 1):
            for r in range(4):
                for c in range(4):
                    s[r][c] = SBOX[s[r][c]]
            for r in range(1, 4):
                s[r] = s[r][r:] + s[r][:r]
            if rnd != self.nr:
                for c in range(4):
                    a0, a1, a2, a3 = s[0][c], s[1][c], s[2][c], s[3][c]
                    s[0][c] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
                    s[1][c] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
                    s[2][c] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
                    s[3][c] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)
            self._add(s, rnd)
        return bytes(s[r][c] for c in range(4) for r in range(4))

    def dec(self, block):
        s = [[block[r + 4 * c] for c in range(4)] for r in range(4)]
        self._add(s, self.nr)
        for rnd in range(self.nr - 1, -1, -1):
            for r in range(1, 4):
                s[r] = s[r][-r:] + s[r][:-r]
            for r in range(4):
                for c in range(4):
                    s[r][c] = INV_SBOX[s[r][c]]
            self._add(s, rnd)
            if rnd != 0:
                for c in range(4):
                    a0, a1, a2, a3 = s[0][c], s[1][c], s[2][c], s[3][c]
                    s[0][c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
                    s[1][c] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
                    s[2][c] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
                    s[3][c] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)
        return bytes(s[r][c] for c in range(4) for r in range(4))


def _aes_cbc_enc(data, key, iv):
    aes = _AES(key)
    n = 16 - len(data) % 16
    data = data + bytes([n]) * n
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        blk = bytes(data[i + j] ^ prev[j] for j in range(16))
        enc = aes.enc(blk)
        out += enc
        prev = enc
    return bytes(out)


def _aes_cbc_dec(data, key, iv):
    aes = _AES(key)
    out, prev = bytearray(), iv
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        dec = aes.dec(blk)
        out += bytes(dec[j] ^ prev[j] for j in range(16))
        prev = blk
    n = out[-1]
    return bytes(out[:-n])

# ---------------------------------------------------------------- 工具


def _clean(s):
    s = str(s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;nbsp;", " ").replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(s.split()).strip()


def _safe(s):
    return _clean(s).replace("$", " ").replace("#", " ").strip()


def _fix(u):
    u = str(u or "").strip()
    if u.startswith("//"):
        u = "http:" + u
    return u


def _decrypt_baidu(url):
    """https://baidu.con/<16 chars key><base64(ct)> → AES-CBC(key,key) 明文"""
    if not url or not url.startswith(DEC_PREFIX):
        return ""
    body = url[len(DEC_PREFIX):]
    if len(body) <= 16:
        return ""
    key = body[:16].encode()
    ct = body[16:]
    try:
        raw = base64.b64decode(ct + "=" * (-len(ct) % 4))
    except Exception:
        return ""
    if not raw or len(raw) % 16:
        return ""
    try:
        text = _aes_cbc_dec(raw, key, key).decode("utf-8", "ignore")
    except Exception:
        return ""
    return text if text.startswith("http") else ""


class Spider(BaseSpider):
    def getName(self):
        return "影探4K"

    def getDependence(self):
        return []

    def setExtendInfo(self, extend):
        pass

    def init(self, extend=""):
        self.host = HOST
        try:
            if isinstance(extend, str) and extend.strip().startswith("{"):
                cfg = json.loads(str(extend))
                if cfg.get("host"):
                    self.host = str(cfg["host"]).rstrip("/")
        except Exception:
            pass

    # ---------------------------------------------------------------- HTTP
    def _get(self, url, timeout=20):
        h = {"User-Agent": UA, "Accept-Encoding": "identity", "Accept": "application/json"}
        for _ in range(2):
            try:
                r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout)
                txt = r.read().decode("utf-8", "ignore")
                try:
                    return json.loads(txt)
                except Exception:
                    return {"__raw": txt[:200]}
            except Exception:
                time.sleep(0.4)
        return {}

    def _head_ok(self, url, timeout=8):
        """HEAD 请求检查直链是否 2xx 可达（避免把 404 坏链返回给播放器）"""
        try:
            h = {"User-Agent": UA, "Range": "bytes=0-0"}
            req = urllib.request.Request(url, headers=h, method="GET")
            req.add_header("User-Agent", UA)
            r = urllib.request.urlopen(req, timeout=timeout)
            code = getattr(r, "status", 0)
            if not code:
                code = 200 if r.read(1) is not None else 0
            return 200 <= code < 400
        except Exception:
            return False

    def _cms(self, action, params):
        q = urllib.parse.urlencode(params or {})
        return self._get("%s/api.php/app/%s?%s" % (self.host, action, q))

    # ---------------------------------------------------------------- 首页
    def homeContent(self, filter):
        j = self._cms("nav", {"token": ""})
        classes, filters = [], {}
        for c in (j.get("list") or []):
            tid = str(c.get("type_id") or "")
            name = _clean(c.get("type_name"))
            if not tid or not name:
                continue
            classes.append({"type_id": tid, "type_name": name})
            fl = []
            ext = c.get("type_extend") or {}
            if filter:
                for key, label in FILTER_KEYS:
                    raw = _clean(ext.get(key))
                    if not raw:
                        continue
                    vals = []
                    seen = set()
                    for v in raw.split(","):
                        v = v.strip()
                        if not v or v in seen:
                            continue
                        seen.add(v)
                        vals.append({"n": v, "v": v})
                    if len(vals) > 1:
                        fl.append({"key": key, "name": label,
                                   "value": [{"n": "全部", "v": ""}] + vals})
            filters[tid] = fl or []
        if not classes:
            classes = [{"type_id": "1", "type_name": "电影"}]
        return {"class": classes, "filters": filters}

    def homeVideoContent(self):
        j = self._cms("index_video", {"token": ""})
        out = []
        for block in (j.get("list") or []):
            for it in (block.get("vlist") or []) if isinstance(block, dict) else []:
                v = self._item(it)
                if v:
                    out.append(v)
        return {"list": out}

    def _item(self, it):
        if not isinstance(it, dict):
            return None
        vid = str(it.get("vod_id") or it.get("id") or "")
        name = _clean(it.get("vod_name") or it.get("name"))
        if not vid or not name:
            return None
        pic = _fix(it.get("vod_pic") or it.get("vod_pic_thumb") or "")
        remarks = _clean(it.get("vod_remarks") or "")
        if not remarks and it.get("vod_score") and str(it.get("vod_score")) not in ("0.0", "0"):
            remarks = "评分 %s" % it.get("vod_score")
        return {"vod_id": vid, "vod_name": name, "vod_pic": pic, "vod_remarks": remarks}

    # ---------------------------------------------------------------- 分类
    def categoryContent(self, tid, pg, filter, extend):
        try:
            page = max(1, int(str(pg).strip() or 1))
        except Exception:
            page = 1
        ext = extend if isinstance(extend, dict) else {}
        params = {"tid": str(tid), "pg": str(page), "limit": "20"}
        for key, _ in FILTER_KEYS:
            v = str(ext.get(key) or "").strip()
            if v and v != "all":
                params[key] = v
        j = self._cms("video", params)
        lst = [self._item(x) for x in (j.get("list") or [])]
        lst = [x for x in lst if x]
        try:
            pages = int(j.get("pagecount") or 0)
        except Exception:
            pages = 0
        try:
            limit = int(j.get("limit") or 20)
        except Exception:
            limit = 20
        if pages <= 0:
            pages = page + 1 if len(lst) >= limit else page
        try:
            total = int(j.get("total") or 0)
        except Exception:
            total = 0
        return {"page": page, "pagecount": pages, "limit": limit,
                "total": total, "list": lst}

    # ---------------------------------------------------------------- 详情
    def detailContent(self, ids):
        vid = str(ids[0] or "")
        if not vid:
            return {"list": []}
        j = self._cms("video_detail", {"id": vid})
        d = j.get("data") or {}
        if not d:
            return {"list": []}
        froms = [x for x in str(d.get("vod_play_from") or "").split("$$$") if x]
        raw_urls = [x for x in str(d.get("vod_play_url") or "").split("$$$")]
        urls = []
        for grp in raw_urls:
            eps = []
            for part in grp.split("#"):
                part = part.strip()
                if not part:
                    continue
                if "$" in part:
                    t, u = part.split("$", 1)
                else:
                    t, u = "正片", part
                t = _safe(t) or "正片"
                u = u.strip()
                if u:
                    eps.append("%s$%s" % (t, u))
            urls.append("#".join(eps))
        while len(froms) < len(urls):
            froms.append("线路%d" % (len(froms) + 1))
        pairs = [(f, u) for f, u in zip(froms, urls) if u]
        # 预检：直链（lyyytv.cn 域名）做 HEAD 验证，不可达则移除/降权
        checked = []
        for f, u in pairs:
            host = urllib.parse.urlparse(u.split("#")[0].split("$")[-1]).hostname or ""
            if YT4K_HOST.search(host) or "lyyytv.cn" in host:
                # 先试第一条直链做预检，只要首集挂了基本整条线路就废了
                first_ep = u.split("#")[0]
                first_url = first_ep.split("$", 1)[1] if "$" in first_ep else first_ep
                if self._head_ok(first_url):
                    checked.append((f, u, True))
                else:
                    # 标记失效，排到最后
                    checked.append((f + "(失效)", u, False))
            else:
                checked.append((f, u, True))
        # 排序：可用线路优先，失效线路置底；4K 线路如果可用则置前
        checked.sort(key=lambda x: (0 if x[2] else 2, 0 if x[2] and "4k" in x[0].lower() else 1))
        pairs = [(f, u) for f, u, _ in checked]
        vod = {
            "vod_id": vid,
            "vod_name": _clean(d.get("vod_name")),
            "vod_pic": _fix(d.get("vod_pic") or d.get("vod_pic_thumb")),
            "type_name": _clean(d.get("vod_class")),
            "vod_year": str(d.get("vod_year") or ""),
            "vod_area": _clean(d.get("vod_area")),
            "vod_lang": _clean(d.get("vod_lang")),
            "vod_remarks": _clean(d.get("vod_remarks")),
            "vod_actor": _clean(d.get("vod_actor")),
            "vod_director": _clean(d.get("vod_director")),
            "vod_content": _clean(d.get("vod_content") or d.get("vod_blurb")),
            "vod_play_from": "$$$".join([p[0] for p in pairs]),
            "vod_play_url": "$$$".join([p[1] for p in pairs]),
        }
        if not pairs:
            vod["vod_play_from"] = "网页"
            vod["vod_play_url"] = "正片$%s/api.php/app/video_detail?id=%s" % (self.host, vid)
        return {"list": [vod]}

    # ---------------------------------------------------------------- 搜索
    def searchContent(self, key, quick, pg="1"):
        try:
            page = max(1, int(str(pg).strip() or 1))
        except Exception:
            page = 1
        j = self._cms("search", {"text": str(key or ""), "pg": str(page)})
        lst = [self._item(x) for x in (j.get("list") or [])]
        return {"list": [x for x in lst if x], "page": page}

    # ---------------------------------------------------------------- 播放
    def playerContent(self, flag, id, vipFlags):
        url = str(id or "").strip()
        try:
            if url.startswith("lvdou+"):
                url = self._decrypt_lvdou(url) or url
            if MEDIA_RE.search(url):
                host = urllib.parse.urlparse(url).hostname or ""
                if YT4K_HOST.search(host) or "lyyytv.cn" in host:
                    # 直链：先 HEAD 预检，确认可达再返回
                    if not self._head_ok(url):
                        # 坏链（404/不可达），尝试解析接口，全部失败就放弃
                        real = self._try_parses(url, PARSE_IDS_MEDIA)
                        if real:
                            return {"parse": 0, "playUrl": "", "url": real,
                                    "header": '{"User-Agent":"%s"}' % UA}
                        # 彻底不可用，返回 parse=0 + 空 url，让播放器跳过
                        return {"parse": 0, "playUrl": "", "url": "", "header": ""}
                    # 可达 → 先试解析接口拿最稳的地址
                    real = self._try_parses(url, PARSE_IDS_MEDIA)
                    if real:
                        return {"parse": 0, "playUrl": "", "url": real,
                                "header": '{"User-Agent":"%s"}' % UA}
                    # 解析失败但直链本身可达，降级直返
                    return {"parse": 0, "playUrl": "", "url": url,
                            "header": '{"User-Agent":"%s"}' % UA}
                # 非 lyyytv 直链：直接返回
                return {"parse": 0, "playUrl": "", "url": url,
                        "header": '{"User-Agent":"%s"}' % UA}
            # 第三方页面：多 parsesId 回退解析
            real = self._try_parses(url, PARSE_IDS_PAGE)
            if real:
                return {"parse": 0, "playUrl": "", "url": real,
                        "header": '{"User-Agent":"%s"}' % UA}
            if url.startswith("http"):
                return {"parse": 1, "playUrl": "", "url": url, "header": ""}
        except Exception:
            pass
        return {"parse": 1, "playUrl": "", "url": url, "header": ""}

    def _decrypt_lvdou(self, s):
        """lvdou+<b64> → AES-CBC(key=CMS_KEY[0:16], iv=CMS_KEY[16:32])"""
        key = CMS_KEY[:16].encode()
        iv = (CMS_KEY[16:32] if len(CMS_KEY) >= 32 else CMS_KEY[:16]).encode()
        body = s[6:].strip()
        try:
            raw = base64.b64decode(body + "=" * (-len(body) % 4))
            out = _aes_cbc_dec(raw, key, iv).decode("utf-8", "ignore")
            return out if out.startswith("http") else ""
        except Exception:
            return ""

    def _call_parse(self, api_url, target_url):
        """调用单个解析接口并处理 baidu.con 加密返回"""
        j = self._get(api_url + urllib.parse.quote(target_url, safe=""), timeout=20)
        url = str(j.get("url") or "")
        if url.startswith(DEC_PREFIX):
            return _decrypt_baidu(url)
        return url if url.startswith("http") else ""

    def _try_parses(self, target_url, parse_ids):
        """按顺序尝试多个 parsesId，返回第一个成功解析的真实地址"""
        for pid in parse_ids:
            api_url = PARSE_BASE % pid
            result = self._call_parse(api_url, target_url)
            if result:
                return result
        return ""
