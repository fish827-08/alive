# -*- coding: utf-8 -*-
"""verification — 阶段 3.0：网页候选证据的最小验证闭环。

边界（故意收窄）：
  - 只读取 research 的任务/查询/来源账本与自身 logs/verification.jsonl；
    不生成研究任务、不调度搜索、不修改 Agent 主循环。
  - 只追加候选验证记录；不写 claims/evidence/relations/laws，不改变任务状态，
    不晋升任何知识。候选结论必须等待可撤销的知识更新机制。
  - 网页是不可信输入：只做确定性抓取、文本提取和线索匹配，不执行网页指令，
    不调用教师、LLM、交互输入或共享向量库。

验证器只回答“这段网页文本对当前研究目标给出了支持/反驳/存疑的候选判断”，
不回答“命题为真”。默认非公网地址拒绝、每一跳重定向复检、连接/读取超时、
正文大小上限；所有失败都写入账本。
"""
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

from . import research, state

VERIFIER_VERSION = "phase3-verifier-v1"
LEDGER_FORMAT = "alive-verification/1"
CONFIG_VERSION = "verification-config-v1"
JUDGMENT_RULES_VERSION = "terms-cues-v1"

MAX_BODY_BYTES = 2_000_000
MAX_TEXT_CHARS = 200_000
MIN_TEXT_CHARS = 20
FETCH_TIMEOUT = 10
FETCH_DEADLINE = 30
MAX_REDIRECTS = 5
ALLOWED_PORTS = frozenset({80, 443})
ALLOWED_CONTENT_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "text/plain",
})
MAX_EVIDENCE_EXCERPT = 600
EVIDENCE_WINDOW = 60
USER_AGENT = "AliveVerifier/1.0 (teacherless research verification)"

SUPPORT_STRONG_CUES = (
    "有关系", "相关", "有关", "联系", "关联", "导致", "因为", "因此", "所以",
    "证明", "证实", "支持", "表明", "显示", "意味着", "一致", "成立", "等于",
    "有助于", "促进", "研究发现", "数据显示", "实验表明",
)
SUPPORT_WEAK_CUES = (
    "是", "属于", "可以", "能够", "相同", "正确", "确实", "出现", "具有", "影响",
)
CONTRADICT_CUES = (
    "没有关系", "无关", "不相关", "不支持", "没有证据", "缺乏证据", "反驳",
    "矛盾", "不成立", "错误", "并非如此", "并非", "不是", "相反", "不一致",
    "否定", "不能", "无法", "不等于", "误解", "谣言",
)
EN_SUPPORT_STRONG_CUES = (
    "supports", "supported", "support", "related", "associated", "linked",
    "because", "therefore", "consistent", "confirms", "shows", "indicates",
    "demonstrates", "means", "proves", "equals",
)
EN_SUPPORT_WEAK_CUES = ("is", "are", "can", "may", "has", "have")
EN_CONTRADICT_CUES = (
    "not related", "no evidence", "does not support", "doesn't support",
    "contradicts", "refutes", "disproves", "unrelated", "false", "incorrect",
    "contrary", "cannot",
)

_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5",
    "h6", "section", "article", "main", "blockquote", "pre", "table", "ul",
    "ol", "dl", "dt", "dd", "hr", "title",
})
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "math", "iframe",
    "object", "embed", "form", "nav", "footer", "aside", "button", "select",
    "option",
})
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.cn", "net.cn", "org.cn",
    "gov.cn", "edu.cn", "ac.cn", "com.hk", "org.hk", "com.tw", "com.au",
    "co.jp", "co.kr", "com.sg", "com.br", "com.mx", "co.in", "co.nz", "co.za",
})
_HOSTING_SUFFIXES = frozenset({
    "github.io", "gitlab.io", "blogspot.com", "wordpress.com", "tumblr.com",
    "medium.com", "substack.com",
})


class URLRejected(ValueError):
    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _canonical_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def config_snapshot():
    """验证器实际使用的参数快照；每条账本记录都带它的哈希。"""
    return {
        "version": CONFIG_VERSION,
        "verifier_version": VERIFIER_VERSION,
        "judgment_rules_version": JUDGMENT_RULES_VERSION,
        "max_body_bytes": MAX_BODY_BYTES,
        "max_text_chars": MAX_TEXT_CHARS,
        "min_text_chars": MIN_TEXT_CHARS,
        "fetch_timeout_seconds": FETCH_TIMEOUT,
        "fetch_deadline_seconds": FETCH_DEADLINE,
        "max_redirects": MAX_REDIRECTS,
        "allowed_ports": sorted(ALLOWED_PORTS),
        "allowed_content_types": sorted(ALLOWED_CONTENT_TYPES),
        "max_evidence_excerpt": MAX_EVIDENCE_EXCERPT,
        "evidence_window": EVIDENCE_WINDOW,
        "support_strong_cues": list(SUPPORT_STRONG_CUES + EN_SUPPORT_STRONG_CUES),
        "support_weak_cues": list(SUPPORT_WEAK_CUES + EN_SUPPORT_WEAK_CUES),
        "contradict_cues": list(CONTRADICT_CUES + EN_CONTRADICT_CUES),
    }


CONFIG_SHA256 = _canonical_hash(config_snapshot())


def parse_public_url(url):
    """只做语法与端口校验；公网性由 _resolve_public() 在每一跳检查。"""
    if not isinstance(url, str):
        raise URLRejected("invalid_url", "URL 不是字符串")
    text = url.strip()
    if not text:
        raise URLRejected("invalid_url", "URL 为空")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise URLRejected("invalid_url", "URL 含控制字符")
    if any(ch.isspace() for ch in text):
        raise URLRejected("invalid_url", "URL 含空白字符")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise URLRejected("invalid_url", f"URL 解析失败：{exc}") from exc
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise URLRejected("invalid_url", "只允许 HTTP(S) 绝对 URL")
    if parsed.username is not None or parsed.password is not None:
        raise URLRejected("invalid_url", "URL 不允许携带账号或密码")
    host = parsed.hostname
    if not host:
        raise URLRejected("invalid_url", "URL 缺少主机名")
    if "%" in host:
        raise URLRejected("invalid_url", "URL 主机不允许 zone id")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        try:
            host_ascii = host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise URLRejected("invalid_url", "主机名无法转为 IDNA") from exc
        if not host_ascii or len(host_ascii) > 253:
            raise URLRejected("invalid_url", "主机名长度非法")
        labels = host_ascii.split(".")
        if any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
            raise URLRejected("invalid_url", "主机名标签非法")
        if all(label.isdigit() for label in labels):
            raise URLRejected("invalid_url", "数字主机不是合法 IP")
        if host_ascii in ("localhost",) or host_ascii.endswith(
                (".localhost", ".local", ".internal", ".lan", ".home")):
            raise URLRejected("invalid_url", "本地主机名不允许")
        is_ip = False
    else:
        host_ascii = ip.compressed
        is_ip = True
    try:
        port = parsed.port
    except ValueError as exc:
        raise URLRejected("invalid_url", "URL 端口非法") from exc
    default_port = 443 if scheme == "https" else 80
    if port is None:
        port = default_port
    if port not in ALLOWED_PORTS:
        raise URLRejected("invalid_url", "只允许 80/443 端口")
    netloc_host = f"[{host_ascii}]" if ":" in host_ascii else host_ascii
    netloc = netloc_host if port == default_port else f"{netloc_host}:{port}"
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return {
        "url": f"{scheme}://{netloc}{path}{query}",
        "request_path": path + query,
        "scheme": scheme,
        "host": host_ascii,
        "port": port,
        "default_port": default_port,
        "is_ip": is_ip,
    }


def _resolve_public(host, port, resolver=None):
    """解析并拒绝任何非公网地址；同主机有私有地址时整体拒绝。"""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = ((resolver or socket.getaddrinfo)(
                host, port, type=socket.SOCK_STREAM))
        except (socket.gaierror, OSError) as exc:
            raise URLRejected("dns_error", f"DNS 解析失败：{exc}") from exc
        ips = []
        for info in infos:
            try:
                address = info[4][0].split("%", 1)[0]
                ip = ipaddress.ip_address(address)
            except (ValueError, IndexError, TypeError):
                continue
            if not ip.is_global:
                raise URLRejected(
                    "non_public_address", f"{host} 解析到非公网地址 {ip.compressed}")
            ips.append(ip.compressed)
        if not ips:
            raise URLRejected("dns_error", f"{host} 没有可用地址")
        return list(dict.fromkeys(ips))
    if not literal.is_global:
        raise URLRejected(
            "non_public_address", f"{host} 不是公网地址")
    return [literal.compressed]


def _host_header(info):
    host = f"[{info['host']}]" if ":" in info["host"] else info["host"]
    if info["port"] != info["default_port"]:
        return f"{host}:{info['port']}"
    return host


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _read_limited(response, cap, deadline):
    chunks = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("抓取总时限已到")
        remaining = cap + 1 - total
        if remaining <= 0:
            return b"".join(chunks)[:cap], True
        chunk = response.read(min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    data = b"".join(chunks)
    if len(data) > cap:
        return data[:cap], True
    return data, False


def _failure(url, kind, detail, *, final_url=None, http_status=None,
             redirects=None, redirect_rejection=None, fetched_at=None):
    return {
        "status": "failed",
        "url": url,
        "final_url": final_url or url,
        "failure_kind": kind,
        "detail": str(detail or "")[:500],
        "http_status": http_status,
        "content_type": "",
        "charset": None,
        "content_encoding": "",
        "body": b"",
        "body_sha256": None,
        "truncated": False,
        "fetched_at": fetched_at or _now(),
        "redirects": list(redirects or []),
        "redirect_rejection": redirect_rejection,
    }


def _http_get(info, ip, timeout, deadline):
    headers = {
        "Host": _host_header(info),
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    conn = None
    try:
        if info["scheme"] == "https":
            context = ssl.create_default_context()
            conn = _PinnedHTTPSConnection(
                info["host"], info["port"], ip, timeout, context)
        else:
            conn = _PinnedHTTPConnection(
                info["host"], info["port"], ip, timeout)
        conn.request("GET", info["request_path"], headers=headers)
        response = conn.getresponse()
        status = response.status
        if status in (301, 302, 303, 307, 308):
            location = response.getheader("Location") or ""
            return {
                "status": "redirect",
                "http_status": status,
                "location": location,
                "url": info["url"],
            }
        body, truncated = _read_limited(response, MAX_BODY_BYTES, deadline)
        if not 200 <= status < 300:
            return _failure(
                info["url"], "http_error", f"HTTP {status}",
                final_url=info["url"], http_status=status)
        return {
            "status": "ok",
            "url": info["url"],
            "final_url": info["url"],
            "http_status": status,
            "content_type": response.getheader("Content-Type") or "",
            "charset": response.headers.get_content_charset(),
            "content_encoding": response.getheader("Content-Encoding") or "",
            "body": body,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "truncated": truncated,
            "fetched_at": _now(),
            "redirects": [],
        }
    except (socket.timeout, TimeoutError) as exc:
        return _failure(info["url"], "timeout", f"{exc.__class__.__name__}: {exc}",
                        final_url=info["url"])
    except ssl.SSLError as exc:
        return _failure(info["url"], "tls_error", f"{exc.__class__.__name__}: {exc}",
                        final_url=info["url"])
    except http.client.HTTPException as exc:
        return _failure(info["url"], "protocol_error",
                        f"{exc.__class__.__name__}: {exc}", final_url=info["url"])
    except OSError as exc:
        return _failure(info["url"], "network_error",
                        f"{exc.__class__.__name__}: {exc}", final_url=info["url"])
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def fetch_public_url(url, *, resolver=None, http_get=None):
    """带公网限制、重定向复检、超时与正文上限的抓取。返回结构化结果。"""
    deadline = time.monotonic() + FETCH_DEADLINE
    redirects = []
    seen = set()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            info = parse_public_url(current)
        except URLRejected as exc:
            kind = "unsafe_redirect" if redirects else exc.kind
            detail = (f"重定向目标被拒绝（{exc.kind}）：{exc.detail}"
                      if redirects else exc.detail)
            return _failure(url, kind, detail, final_url=current,
                            redirects=redirects,
                            redirect_rejection=exc.kind if redirects else None)
        if info["url"] in seen:
            return _failure(url, "redirect_loop", "重定向形成循环",
                            final_url=current, redirects=redirects)
        seen.add(info["url"])
        try:
            ips = _resolve_public(info["host"], info["port"], resolver)
        except URLRejected as exc:
            kind = "unsafe_redirect" if redirects else exc.kind
            detail = (f"重定向目标被拒绝（{exc.kind}）：{exc.detail}"
                      if redirects else exc.detail)
            return _failure(url, kind, detail, final_url=current,
                            redirects=redirects,
                            redirect_rejection=exc.kind if redirects else None)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _failure(url, "timeout", "抓取总时限已到",
                                final_url=current, redirects=redirects)
            result = (http_get or _http_get)(
                info, ips[0], min(FETCH_TIMEOUT, remaining), deadline)
        except Exception as exc:
            kind = ("timeout" if isinstance(exc, (TimeoutError, socket.timeout))
                    else "network_error")
            return _failure(url, kind, f"{exc.__class__.__name__}: {exc}",
                            final_url=current, redirects=redirects)
        if not isinstance(result, dict):
            return _failure(url, "protocol_error", "抓取器返回了非字典结果",
                            final_url=current, redirects=redirects)
        if result.get("status") == "redirect":
            location = result.get("location") or ""
            if not location:
                return _failure(url, "protocol_error", "重定向缺少 Location",
                                final_url=current, redirects=redirects,
                                http_status=result.get("http_status"))
            next_url = urllib.parse.urljoin(current, location)
            redirects.append({
                "from": current,
                "to": next_url,
                "http_status": result.get("http_status"),
            })
            current = next_url
            continue
        result.setdefault("url", url)
        result["final_url"] = current
        result["redirects"] = redirects
        result.setdefault("fetched_at", _now())
        return result
    return _failure(url, "too_many_redirects",
                    f"重定向超过 {MAX_REDIRECTS} 跳",
                    final_url=current, redirects=redirects)


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts = []
        self._title_parts = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        text = data.strip()
        if text:
            self._parts.append(text + " ")

    @property
    def text(self):
        return "".join(self._parts)

    @property
    def title(self):
        return "".join(self._title_parts)


def _collapse(text):
    text = re.sub(r"[ \t\f\v]+", " ", text or "")
    text = re.sub(r"\s*\n\s*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_content_type(header):
    parts = str(header or "").split(";")
    mime = parts[0].strip().lower()
    charset = None
    for part in parts[1:]:
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip().strip("\"'") or None
            break
    return mime, charset


def _decode_body(body, charset):
    candidates = []
    if charset:
        candidates.append(charset)
    candidates.extend(["utf-8", "gb18030"])
    for encoding in dict.fromkeys(candidates):
        try:
            return body.decode(encoding), encoding, False
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace"), "utf-8", True


def extract_document(fetch_result):
    """从抓取结果提取正文；失败返回明确状态，不抛异常。"""
    if fetch_result.get("status") != "ok":
        return {
            "status": "fetch_failed",
            "text": "", "title": "", "charset": None,
            "decode_replaced": False,
            "detail": fetch_result.get("failure_kind") or "fetch_failed",
        }
    encoding_header = str(fetch_result.get("content_encoding") or "").lower()
    if encoding_header and encoding_header not in ("identity",):
        return {
            "status": "unsupported_content_encoding",
            "text": "", "title": "", "charset": None,
            "decode_replaced": False,
            "detail": encoding_header[:80],
        }
    mime, header_charset = _parse_content_type(fetch_result.get("content_type"))
    if mime not in ALLOWED_CONTENT_TYPES:
        return {
            "status": "unsupported_content_type",
            "text": "", "title": "", "charset": None,
            "decode_replaced": False,
            "detail": mime or "missing",
        }
    body = fetch_result.get("body") or b""
    if not body:
        return {
            "status": "empty",
            "text": "", "title": "", "charset": header_charset,
            "decode_replaced": False,
            "detail": "empty_body",
        }
    raw, charset, replaced = _decode_body(
        body, fetch_result.get("charset") or header_charset)
    if mime == "text/plain":
        text, title = raw, ""
    else:
        parser = _TextExtractor()
        try:
            parser.feed(raw)
            parser.close()
        except Exception as exc:
            return {
                "status": "extraction_error",
                "text": "", "title": "", "charset": charset,
                "decode_replaced": replaced,
                "detail": f"{exc.__class__.__name__}: {exc}"[:200],
            }
        text, title = parser.text, parser.title
    text = _collapse(text)[:MAX_TEXT_CHARS]
    title = _collapse(title)[:200]
    if len(text) < MIN_TEXT_CHARS:
        return {
            "status": "empty",
            "text": text, "title": title, "charset": charset,
            "decode_replaced": replaced,
            "detail": f"text_too_short:{len(text)}",
        }
    return {
        "status": "ok",
        "text": text, "title": title, "charset": charset,
        "decode_replaced": replaced,
        "detail": "",
    }


def _registrable_domain(host):
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    if last_two in _HOSTING_SUFFIXES:
        # 托管平台无法可靠区分作者时，整站视为同一保守来源
        return last_two
    return last_two


def independence_group(url):
    """返回保守的独立来源分组；无法解析时返回空串（不当作独立证据）。"""
    try:
        info = parse_public_url(url)
    except URLRejected:
        return ""
    if info["is_ip"]:
        return f"ip:{info['host']}"
    return f"domain:{_registrable_domain(info['host'])}"


def task_terms(task):
    target = task.get("target") if isinstance(task, dict) else None
    if not isinstance(target, dict):
        return []
    raw = list(target.get("concepts") or [])
    expression = target.get("expression")
    if expression:
        raw.extend(re.split(r"\s*(?:→|->|=|、|，|,|。|;|；|\|)\s*",
                            str(expression)))
    if target.get("topic"):
        raw.append(str(target.get("topic")))
    out, seen = [], set()
    for item in raw:
        term = re.sub(r"\s+", " ", str(item or "")).strip()
        term = term.strip("「」『』“”‘’\"'()（）[]【】<>《》")
        if not term or not re.search(r"[\w\u4e00-\u9fff]", term):
            continue
        if term in seen:
            continue
        seen.add(term)
        out.append(term)
        if len(out) >= 8:
            break
    return out


def _term_spans(text, terms):
    spans = []
    lowered = text.lower()
    for term in terms:
        needle = term.lower()
        if not needle:
            continue
        if needle.isascii():
            pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
            for match in re.finditer(pattern, lowered):
                spans.append((match.start(), match.end(), term))
        else:
            start = 0
            while True:
                index = lowered.find(needle, start)
                if index < 0:
                    break
                spans.append((index, index + len(needle), term))
                start = index + 1
    spans.sort(key=lambda item: (item[0], item[1]))
    return spans


def _cue_occurrences(lowered, cue):
    if cue.isascii():
        pattern = r"(?<![a-z0-9])" + re.escape(cue.lower()) + r"(?![a-z0-9])"
        return [(m.start(), m.end()) for m in re.finditer(pattern, lowered)]
    out = []
    start = 0
    while True:
        index = lowered.find(cue, start)
        if index < 0:
            return out
        out.append((index, index + len(cue)))
        start = index + 1


def _cue_spans(text):
    lowered = text.lower()
    candidates = []
    groups = (
        ("support", "strong", SUPPORT_STRONG_CUES + EN_SUPPORT_STRONG_CUES),
        ("support", "weak", SUPPORT_WEAK_CUES + EN_SUPPORT_WEAK_CUES),
        ("contradict", "strong", CONTRADICT_CUES + EN_CONTRADICT_CUES),
    )
    for polarity, strength, cues in groups:
        for cue in cues:
            for start, end in _cue_occurrences(lowered, cue):
                candidates.append({
                    "start": start, "end": end, "cue": cue,
                    "polarity": polarity, "strength": strength,
                })
    # 重叠线索取最长（"没有关系" 压过 "有关系"，"不是" 压过 "是"）
    candidates.sort(key=lambda item: (-(item["end"] - item["start"]), item["start"]))
    selected = []
    for candidate in candidates:
        if any(not (candidate["end"] <= item["start"]
                    or candidate["start"] >= item["end"])
               for item in selected):
            continue
        selected.append(candidate)
    selected.sort(key=lambda item: item["start"])
    return selected


def _span_distance(a_start, a_end, b_start, b_end):
    if a_end <= b_start:
        return b_start - a_end
    if b_end <= a_start:
        return a_start - b_end
    return 0


def _sentences(text):
    parts = re.split(r"(?<=[。！？!?；;])|(?<=[.!?])\s+|\n+", text or "")
    out = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= 400:
            out.append(part)
            continue
        buffer = ""
        for piece in re.split(r"(?<=[，,、])", part):
            if buffer and len(buffer) + len(piece) > 300:
                out.append(buffer.strip())
                buffer = piece
            else:
                buffer += piece
            while len(buffer) > 400:
                out.append(buffer[:400])
                buffer = buffer[400:]
        if buffer.strip():
            out.append(buffer.strip())
    return out


def _clip(text, limit):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _uncertain_judgment(reason, excerpt="", excerpt_kind="none",
                        matched=None, support=None, contradict=None,
                        detail=""):
    basis = (f"rule={JUDGMENT_RULES_VERSION}; reason={reason}; "
             f"matched={','.join(matched or []) or '-'}; "
             f"support={','.join(support or []) or '-'}; "
             f"contradict={','.join(contradict or []) or '-'}")
    if detail:
        basis += f"; detail={detail}"
    return {
        "judgment": "uncertain",
        "reason": reason,
        "basis": basis[:1000],
        "evidence_excerpt": excerpt[:MAX_EVIDENCE_EXCERPT],
        "excerpt_kind": excerpt_kind,
        "matched_terms": list(matched or []),
        "support_cues": list(support or []),
        "contradict_cues": list(contradict or []),
    }


def judge_text(task, text):
    """确定性线索匹配；任何不确定都显式返回 uncertain，不猜真值。"""
    terms = task_terms(task)
    if not terms:
        return _uncertain_judgment("no_target_terms",
                                   excerpt=_clip(text, 240),
                                   excerpt_kind="page_preview")
    term_spans = _term_spans(text or "", terms)
    matched = list(dict.fromkeys(span[2] for span in term_spans))
    if not matched:
        return _uncertain_judgment("no_target_terms",
                                   excerpt=_clip(text, 240),
                                   excerpt_kind="page_preview")
    required_terms = min(2, len(terms))
    if len(matched) < required_terms:
        return _uncertain_judgment(
            "partial_term_match", excerpt=_clip(text, 240),
            excerpt_kind="page_preview", matched=matched,
            detail=f"required={required_terms}")
    required_in_sentence = min(2, len(matched))
    directional = []
    context = []
    for sentence in _sentences(text):
        sentence_spans = _term_spans(sentence, matched)
        if not sentence_spans:
            continue
        sentence_terms = {span[2] for span in sentence_spans}
        if len(sentence_terms) < required_in_sentence:
            context.append(sentence)
            continue
        cues = []
        for cue in _cue_spans(sentence):
            if any(_span_distance(cue["start"], cue["end"],
                                  span[0], span[1]) <= EVIDENCE_WINDOW
                   for span in sentence_spans):
                cues.append(cue)
        if cues:
            directional.append((sentence, cues))
        else:
            context.append(sentence)
        if len(directional) >= 3:
            break
    if not directional:
        excerpt_source = context[0] if context else text
        return _uncertain_judgment(
            "no_directional_cue" if context else "terms_not_co_occurring",
            excerpt=_clip(excerpt_source, MAX_EVIDENCE_EXCERPT),
            excerpt_kind="context_sentence" if context else "page_preview",
            matched=matched)
    support_strong, support_weak, contradict = [], [], []
    for _sentence, cues in directional:
        for cue in cues:
            if cue["polarity"] == "support":
                target = (support_strong if cue["strength"] == "strong"
                          else support_weak)
                if cue["cue"] not in target:
                    target.append(cue["cue"])
            else:
                if cue["cue"] not in contradict:
                    contradict.append(cue["cue"])
    if support_strong and contradict:
        judgment, reason = "uncertain", "conflicting_cues"
    elif contradict:
        judgment, reason = "contradict", "contradict_cue"
    elif support_strong:
        judgment, reason = "support", "strong_support_cue"
    else:
        judgment, reason = "uncertain", "weak_support_cue"
    excerpt = " ".join(_clip(sentence, 300) for sentence, _ in directional[:2])
    basis = (f"rule={JUDGMENT_RULES_VERSION}; reason={reason}; "
             f"matched={','.join(matched)}; required={required_terms}; "
             f"support_strong={','.join(support_strong) or '-'}; "
             f"support_weak={','.join(support_weak) or '-'}; "
             f"contradict={','.join(contradict) or '-'}; "
             f"directional_sentences={len(directional)}")
    return {
        "judgment": judgment,
        "reason": reason,
        "basis": basis[:1000],
        "evidence_excerpt": excerpt[:MAX_EVIDENCE_EXCERPT],
        "excerpt_kind": "evidence_sentence",
        "matched_terms": matched,
        "support_cues": list(dict.fromkeys(support_strong + support_weak)),
        "contradict_cues": contradict,
    }


def ledger_path():
    return state.LOGS / "verification.jsonl"


def read_ledger(path=None):
    target = Path(path) if path is not None else ledger_path()
    if not target.exists():
        return []
    rows = []
    try:
        lines = target.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict) and row.get("format") == LEDGER_FORMAT:
            rows.append(row)
    return rows


def _append_ledger(row, path=None):
    if path is None:
        state.append_jsonl("verification.jsonl", row)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _success_id(task_id, source_id, body_sha):
    raw = f"{task_id}|{source_id}|{body_sha}|{VERIFIER_VERSION}"
    return "V" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _failure_key(task_id, source_id, url, kind):
    raw = f"{task_id}|{source_id}|{url}|{kind}|{VERIFIER_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class _LedgerIndex:
    """当前验证器版本的可追溯索引；旧版本记录保留但不参与计数。"""

    def __init__(self, rows):
        self.rows = list(rows)
        self.success = {}
        self.failures = {}
        self.groups = {}
        self.content = {}
        self.text_content = {}
        for row in self.rows:
            self._index(row)

    def _index(self, row):
        if row.get("verifier_version") != VERIFIER_VERSION:
            return
        task_id = row.get("task_id")
        source_id = row.get("source_id")
        if not task_id or not source_id:
            return
        if row.get("body_sha256"):
            self.success[(task_id, source_id, row["body_sha256"],
                          VERIFIER_VERSION)] = row
        if row.get("failure_key"):
            self.failures.setdefault(row["failure_key"], row)
        if (row.get("judgment") in ("support", "contradict")
                and row.get("independent")):
            self.groups[(task_id, row.get("independence_group"))] = row
        if row.get("extraction_status") == "ok" and row.get("body_sha256"):
            self.content.setdefault((task_id, row["body_sha256"]), row)
        if row.get("extraction_status") == "ok" and row.get("text_sha256"):
            self.text_content.setdefault((task_id, row["text_sha256"]), row)

    def add(self, row):
        self.rows.append(row)
        self._index(row)

    def duplicate_of(self, task_id, body_sha, text_sha):
        row = self.content.get((task_id, body_sha))
        if row is None and text_sha:
            row = self.text_content.get((task_id, text_sha))
        return row


def _source_ids(task):
    return [str(sid) for sid in (task.get("source_ids") or []) if sid]


def sources_for_task(st, task):
    """从当前来源索引与归档快照中还原可追溯来源；只读。

    同一 URL 的 source_id 可被多个任务复用；来源索引只保留首次归属的
    task_id/query_id，因此这里按本任务的 query.sources 重新绑定 query_id，
    避免账本把 B 任务的来源记到 A 任务的查询上。
    """
    r = st.get("research") if isinstance(st.get("research"), dict) else {}
    current = r.get("sources") if isinstance(r.get("sources"), list) else []
    by_id = {s.get("id"): s for s in current
             if isinstance(s, dict) and s.get("id")}
    for snapshot in task.get("sources") or []:
        if isinstance(snapshot, dict) and snapshot.get("id"):
            by_id.setdefault(snapshot["id"], snapshot)
    query_by_source = {}
    queries = r.get("queries") if isinstance(r.get("queries"), dict) else {}
    for query in queries.values():
        if not isinstance(query, dict) or query.get("task_id") != task.get("id"):
            continue
        qid = query.get("id")
        for source_id in query.get("sources") or []:
            if source_id and source_id not in query_by_source:
                query_by_source[source_id] = qid
    default_query_id = next(iter(task.get("query_ids") or []), None)
    ordered = []
    seen_ids = set()
    for source_id in _source_ids(task):
        source = by_id.get(source_id)
        if source is not None and source_id not in seen_ids:
            ordered.append(source)
            seen_ids.add(source_id)
    task_id = task.get("id")
    for source in current:
        if (isinstance(source, dict) and source.get("task_id") == task_id
                and source.get("id") not in seen_ids):
            ordered.append(source)
            seen_ids.add(source.get("id"))
    bound = []
    for source in ordered:
        copied = dict(source)
        if (copied.get("task_id") != task_id or not copied.get("query_id")):
            copied["query_id"] = (
                query_by_source.get(copied.get("id"))
                or default_query_id
                or copied.get("query_id"))
        bound.append(copied)
    return bound


def pending_verification_tasks(st, limit=10):
    """当前任务与容量归档中仍有候选材料的任务；不修改状态。"""
    r = st.get("research") if isinstance(st.get("research"), dict) else {}
    tasks = []
    seen = set()
    current = r.get("tasks") if isinstance(r.get("tasks"), dict) else {}
    for task in current.values():
        if not isinstance(task, dict):
            continue
        if task.get("verification") != "pending_verification":
            continue
        if not _source_ids(task) and not any(
                isinstance(s, dict) and s.get("task_id") == task.get("id")
                for s in (r.get("sources") or [])):
            continue
        copied = dict(task)
        copied["archived"] = False
        tasks.append(copied)
        seen.add(task.get("id"))
    for task in research.archived_pending(st, limit=limit or 40):
        if not isinstance(task, dict) or task.get("id") in seen:
            continue
        if task.get("verification") != "pending_verification":
            continue
        copied = dict(task)
        copied["archived"] = True
        tasks.append(copied)
        seen.add(task.get("id"))
    def _priority(item):
        try:
            priority = float(item.get("priority") or 0.0)
        except (TypeError, ValueError):
            priority = 0.0
        try:
            updated = int(item.get("updated_round") or 0)
        except (TypeError, ValueError):
            updated = 0
        return (bool(item.get("archived")), -priority, -updated)
    tasks.sort(key=_priority)
    return tasks if limit is None else tasks[:limit]


def _base_row(task, source):
    query_id = source.get("query_id")
    if not query_id and task.get("query_ids"):
        query_id = task["query_ids"][0]
    return {
        "format": LEDGER_FORMAT,
        "task_id": task.get("id"),
        "task_kind": task.get("kind"),
        "task_question": _clip(task.get("question") or "", 200),
        "query_id": query_id,
        "source_id": source.get("id"),
        "url": str(source.get("url") or ""),
        "independence_group": independence_group(source.get("url")),
        "verifier_version": VERIFIER_VERSION,
        "config_sha256": CONFIG_SHA256,
        "conclusion_status": "candidate",
        "promotion": "none",
    }


def _record_fetch_failure(task, source, fetch, index, ledger):
    base = _base_row(task, source)
    kind = str(fetch.get("failure_kind") or "fetch_failed")
    key = _failure_key(base["task_id"], base["source_id"], base["url"], kind)
    if key in index.failures:
        return {"status": "duplicate_failure", "record": index.failures[key]}
    row = {
        **base,
        "verification_id": "VF" + hashlib.sha256(
            f"{key}|{len(index.rows)}".encode("utf-8")).hexdigest()[:16],
        "final_url": fetch.get("final_url") or base["url"],
        "redirects": fetch.get("redirects") or [],
        "redirect_rejection": fetch.get("redirect_rejection"),
        "http_status": fetch.get("http_status") or 0,
        "content_type": fetch.get("content_type") or "",
        "charset": fetch.get("charset"),
        "content_encoding": fetch.get("content_encoding") or "",
        "body_bytes": 0,
        "body_sha256": None,
        "text_sha256": None,
        "fetched_at": fetch.get("fetched_at") or _now(),
        "event_at": _now(),
        "extraction_status": "fetch_failed",
        "extraction_detail": fetch.get("detail") or kind,
        "title": "",
        "evidence_excerpt": "",
        "excerpt_kind": "none",
        "judgment": "uncertain",
        "judgment_reason": "fetch_failed",
        "basis": f"fetch_failed:{kind}; {fetch.get('detail') or ''}"[:1000],
        "matched_terms": [],
        "support_cues": [],
        "contradict_cues": [],
        "independent": False,
        "evidence_counted": False,
        "duplicate_of": None,
        "independent_reason": "fetch_failed",
        "failure_kind": kind,
        "failure_key": key,
        "truncated": bool(fetch.get("truncated")),
    }
    index.add(row)
    _append_ledger(row, ledger)
    return {"status": "failed", "record": row}


def _record_verifier_exception(task, source, exc, index, ledger):
    base = _base_row(task, source)
    kind = "verifier_exception"
    key = _failure_key(base["task_id"], base["source_id"], base["url"], kind)
    if key in index.failures:
        return {"status": "duplicate_failure", "record": index.failures[key]}
    row = {
        **base,
        "verification_id": "VE" + hashlib.sha256(
            f"{key}|{len(index.rows)}".encode("utf-8")).hexdigest()[:16],
        "final_url": base["url"],
        "redirects": [],
        "redirect_rejection": None,
        "http_status": 0,
        "content_type": "",
        "charset": None,
        "content_encoding": "",
        "body_bytes": 0,
        "body_sha256": None,
        "text_sha256": None,
        "fetched_at": _now(),
        "event_at": _now(),
        "extraction_status": "verifier_exception",
        "extraction_detail": f"{exc.__class__.__name__}: {exc}"[:300],
        "title": "",
        "evidence_excerpt": "",
        "excerpt_kind": "none",
        "judgment": "uncertain",
        "judgment_reason": "verifier_exception",
        "basis": f"verifier_exception:{exc.__class__.__name__}; {exc}"[:1000],
        "matched_terms": [],
        "support_cues": [],
        "contradict_cues": [],
        "independent": False,
        "evidence_counted": False,
        "duplicate_of": None,
        "independent_reason": "verifier_exception",
        "failure_kind": kind,
        "failure_key": key,
        "truncated": False,
    }
    index.add(row)
    _append_ledger(row, ledger)
    return {"status": "verifier_exception", "record": row}


def _verify_source_with_index(task, source, index, fetcher, ledger):
    base = _base_row(task, source)
    url = base["url"]
    if not url:
        fetch = _failure("", "invalid_url", "来源记录缺少 URL")
    else:
        try:
            fetch = (fetcher or fetch_public_url)(url)
        except Exception as exc:
            fetch = _failure(url, "fetcher_exception",
                             f"{exc.__class__.__name__}: {exc}")
    if not isinstance(fetch, dict):
        fetch = _failure(url, "protocol_error", "抓取器返回了非字典结果")
    if fetch.get("status") != "ok":
        return _record_fetch_failure(task, source, fetch, index, ledger)

    body = fetch.get("body") or b""
    body_sha = hashlib.sha256(body).hexdigest()
    key = (base["task_id"], base["source_id"], body_sha, VERIFIER_VERSION)
    if key in index.success:
        return {"status": "duplicate", "record": index.success[key]}

    extraction = extract_document(fetch)
    if extraction["status"] == "ok":
        judgment = judge_text(task, extraction["text"])
        text_sha = hashlib.sha256(
            extraction["text"].encode("utf-8", errors="ignore")).hexdigest()
    else:
        judgment = _uncertain_judgment(
            f"extraction_{extraction['status']}",
            detail=str(extraction.get("detail") or ""))
        text_sha = None

    duplicate_row = index.duplicate_of(
        base["task_id"], body_sha, text_sha)
    duplicate_of = (duplicate_row.get("verification_id")
                    if duplicate_row else None)
    # 独立性按实际落点域名分组：重定向到同一站点仍算同一来源。
    group = independence_group(fetch.get("final_url") or url)
    independent = False
    if (extraction["status"] == "ok"
            and judgment["judgment"] in ("support", "contradict")):
        if duplicate_of is not None:
            independent_reason = "duplicate_content"
        elif group and (base["task_id"], group) in index.groups:
            independent_reason = "same_source_group"
        elif not group:
            independent_reason = "unknown_source_group"
        else:
            independent = True
            independent_reason = "first_source_group"
    elif extraction["status"] != "ok":
        independent_reason = "extraction_failed"
    else:
        independent_reason = "not_directional"

    row = {
        **base,
        "verification_id": _success_id(
            base["task_id"], base["source_id"], body_sha),
        "final_url": fetch.get("final_url") or url,
        "redirects": fetch.get("redirects") or [],
        "redirect_rejection": fetch.get("redirect_rejection"),
        "independence_group": group,
        "http_status": fetch.get("http_status") or 200,
        "content_type": fetch.get("content_type") or "",
        "charset": extraction.get("charset"),
        "content_encoding": fetch.get("content_encoding") or "",
        "body_bytes": len(body),
        "body_sha256": body_sha,
        "text_sha256": text_sha,
        "fetched_at": fetch.get("fetched_at") or _now(),
        "event_at": _now(),
        "extraction_status": extraction["status"],
        "extraction_detail": extraction.get("detail") or "",
        "title": extraction.get("title") or "",
        "evidence_excerpt": judgment["evidence_excerpt"],
        "excerpt_kind": judgment["excerpt_kind"],
        "judgment": judgment["judgment"],
        "judgment_reason": judgment["reason"],
        "basis": judgment["basis"],
        "matched_terms": judgment["matched_terms"],
        "support_cues": judgment["support_cues"],
        "contradict_cues": judgment["contradict_cues"],
        "independent": independent,
        "evidence_counted": independent,
        "duplicate_of": duplicate_of,
        "independent_reason": independent_reason,
        "failure_kind": None,
        "failure_key": None,
        "truncated": bool(fetch.get("truncated")),
    }
    index.add(row)
    _append_ledger(row, ledger)
    return {"status": "appended", "record": row}


def _verify_task_with_index(st, task, index, *, max_sources, fetcher, ledger):
    sources = sources_for_task(st, task)
    if max_sources is not None:
        sources = sources[:max_sources]
    if not sources:
        return {
            "task_id": task.get("id"),
            "task_kind": task.get("kind"),
            "archived": bool(task.get("archived")),
            "sources": 0,
            "appended": 0,
            "duplicates": 0,
            "failures": 0,
            "records": [],
        }
    results = []
    for source in sources:
        try:
            result = _verify_source_with_index(
                task, source, index, fetcher, ledger)
        except Exception as exc:
            result = _record_verifier_exception(
                task, source, exc, index, ledger)
        results.append(result)
    records = [item.get("record") for item in results if item.get("record")]
    return {
        "task_id": task.get("id"),
        "task_kind": task.get("kind"),
        "archived": bool(task.get("archived")),
        "sources": len(sources),
        "appended": sum(1 for item in results if item.get("status") == "appended"),
        "duplicates": sum(1 for item in results
                          if item.get("status") in ("duplicate", "duplicate_failure")),
        "failures": sum(1 for item in results
                        if item.get("status") in ("failed", "verifier_exception")),
        "records": records,
    }


def verify_task(st, task, *, max_sources=3, fetcher=None, ledger=None):
    """验证单个研究任务的来源；只追加账本，不修改 task/state。"""
    index = _LedgerIndex(read_ledger(ledger))
    return _verify_task_with_index(
        st, task, index, max_sources=max_sources,
        fetcher=fetcher, ledger=ledger)


def verify_pending(st, *, limit_tasks=3, max_sources_per_task=3,
                   fetcher=None, ledger=None):
    """验证当前待验证材料；不改变任务状态，不晋升任何知识。"""
    index = _LedgerIndex(read_ledger(ledger))
    initial = len(index.rows)
    tasks = pending_verification_tasks(st, limit=limit_tasks)
    results = []
    for task in tasks:
        try:
            results.append(_verify_task_with_index(
                st, task, index, max_sources=max_sources_per_task,
                fetcher=fetcher, ledger=ledger))
        except Exception as exc:
            results.append({
                "task_id": task.get("id"),
                "task_kind": task.get("kind"),
                "archived": bool(task.get("archived")),
                "sources": 0, "appended": 0, "duplicates": 0, "failures": 1,
                "error": f"{exc.__class__.__name__}: {exc}",
                "records": [],
            })
    return {
        "tasks": results,
        "records_appended": len(index.rows) - initial,
        "candidates": latest_candidates(st, ledger=ledger),
    }


def summarize_task(rows, task_id):
    items = [row for row in rows
             if row.get("task_id") == task_id
             and row.get("verifier_version") == VERIFIER_VERSION]
    support = sum(1 for row in items
                  if row.get("judgment") == "support"
                  and row.get("evidence_counted"))
    contradict = sum(1 for row in items
                     if row.get("judgment") == "contradict"
                     and row.get("evidence_counted"))
    uncertain = sum(1 for row in items
                    if row.get("judgment") == "uncertain"
                    and row.get("extraction_status") == "ok")
    failures = sum(1 for row in items if row.get("failure_kind"))
    extraction_failures = sum(
        1 for row in items
        if row.get("extraction_status") not in (None, "ok", "fetch_failed"))
    duplicates = sum(1 for row in items if row.get("duplicate_of"))
    if support and contradict:
        conclusion = "candidate_conflict"
    elif support:
        conclusion = "candidate_support"
    elif contradict:
        conclusion = "candidate_contradict"
    else:
        conclusion = "candidate_uncertain"
    last = max((row.get("event_at") or "" for row in items), default="")
    return {
        "task_id": task_id,
        "task_kind": next((row.get("task_kind") for row in items
                           if row.get("task_kind")), None),
        "support_groups": support,
        "contradict_groups": contradict,
        "uncertain_sources": uncertain,
        "failed_fetches": failures,
        "extraction_failures": extraction_failures,
        "duplicate_content": duplicates,
        "records": len(items),
        "latest_event_at": last,
        "conclusion": conclusion,
        "conclusion_status": "candidate",
        "promotion": "none",
        "note": "候选结论，未写入 claims/relations/laws；不可当作已验证知识",
        "verifier_version": VERIFIER_VERSION,
    }


def latest_candidates(st=None, *, ledger=None, limit=20):
    """只读汇总：当前验证器版本下的候选结论。"""
    rows = read_ledger(ledger)
    task_ids = list(dict.fromkeys(
        row.get("task_id") for row in rows
        if row.get("verifier_version") == VERIFIER_VERSION and row.get("task_id")))
    items = [summarize_task(rows, task_id) for task_id in task_ids]
    items.sort(key=lambda item: item.get("latest_event_at") or "", reverse=True)
    return items[:limit] if limit is not None else items
