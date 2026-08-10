"""Acquire and normalize source documents — the raw material for `wiki.py`.

The OKF pitch starts from an observation about where organizational knowledge
actually lives: a wiki page, a policy memo, a README, a PDF someone emailed.
This module turns that exhaust into `Document` objects with enough provenance
to become OKF `sources[]` entries — where it came from, who wrote it, when it
last changed, and a hash of exactly what we read.

    docs  = load_paths(["corpus/acme_intranet"])
    docs += crawl(["https://example.com/handbook"], max_pages=10)

Nothing here talks to an LLM or to Neo4j. Extraction is `wiki.py`'s job; this
module's only opinions are about safety and about preserving heading structure,
because OKF's conventional `# headings` are what the parser turns into
retrieval units (SPEC §4.2) — flatten them and the graph gets worse chunks.

Fetching arbitrary URLs is the one genuinely dangerous thing the repo does, so
`FetchPolicy` is deny-by-default: http/https only, robots.txt respected,
private and loopback address space refused, response size capped, redirects
re-validated at every hop.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import ipaddress
import re
import socket
import time
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, urlunparse

DEFAULT_USER_AGENT = "okf-graph/0.3 (+https://github.com/johnymontana/neo4j-okf)"
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | HTML_SUFFIXES | PDF_SUFFIXES

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_HEADING_RE = re.compile(r"^(#{1,6})\s", re.MULTILINE)


@dataclass
class Document:
    """One acquired document, with the provenance OKF §5.1 wants."""

    uid: str
    origin: str                       # 'file' | 'web'
    location: str                     # filesystem path or absolute URL
    title: str
    text: str                         # markdown (headings preserved)
    slug: str                         # bundle-path-safe identifier
    media_type: str = "text/markdown"
    author: Optional[str] = None
    last_modified: Optional[str] = None      # YYYY-MM-DD
    retrieved_at: Optional[str] = None       # ISO 8601
    sha256: str = ""
    byte_len: int = 0
    links: list[str] = field(default_factory=list)   # outbound URLs (crawl frontier)

    @property
    def resource(self) -> str:
        """The value that belongs in `sources[].resource` (SPEC §6.2)."""
        return self.location


class FetchError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def slugify(text: str, fallback: str = "document") -> str:
    slug = _SLUG_STRIP.sub("-", (text or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:80].strip("-")
    return slug or fallback


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _normalize_headings(markdown: str) -> str:
    """Shift heading levels so the shallowest one used becomes `#`.

    A web page usually spends its `<h1>` on the site or article title and puts
    real structure in `<h2>`. The parser splits sections on `# ` only, so
    without this every page would arrive as one undifferentiated blob.
    """
    levels = [len(m.group(1)) for m in _HEADING_RE.finditer(markdown)]
    if not levels:
        return markdown
    shift = min(levels) - 1
    if shift <= 0:
        return markdown
    return _HEADING_RE.sub(lambda m: "#" * (len(m.group(1)) - shift) + " ", markdown)


# --------------------------------------------------------------------------
# HTML -> markdown
# --------------------------------------------------------------------------

_DROP_TAGS = ("script", "style", "noscript", "nav", "footer", "header",
              "aside", "form", "iframe", "svg", "template")


def html_to_markdown(html: str, base_url: str = "") -> tuple[str, str, dict]:
    """`(title, markdown, meta)` from an HTML document.

    Boilerplate is stripped and `<main>`/`<article>` preferred, because feeding
    a nav sidebar to an extractor produces concepts about the nav sidebar.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    doc_title = soup.title.string.strip() if (soup.title and soup.title.string) else ""

    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or "").lower()
        content = tag.get("content")
        if key and content:
            meta.setdefault(key, content.strip())

    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: getattr(s, "output_ready", None)
                                 and s.__class__.__name__ == "Comment"):
        comment.extract()

    root = soup.find("main") or soup.find("article") or soup.body or soup
    # prefer the page's own <h1> over <title>, which usually carries site
    # branding ("Gross Margin — Acme Analytics Wiki") we do not want in a title
    h1 = root.find("h1")
    title = (h1.get_text(" ", strip=True) if h1 else "") or doc_title

    lines: list[str] = []
    _render(root, lines, base_url)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return title, _normalize_headings(text), meta


def _inline(node, base_url: str) -> str:
    """Inline markdown for a node's children (links, emphasis, code)."""
    from bs4 import NavigableString

    out: list[str] = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(re.sub(r"\s+", " ", str(child)))
            continue
        name = child.name
        inner = _inline(child, base_url)
        if name == "a":
            href = child.get("href") or ""
            href = urljoin(base_url, href) if base_url else href
            out.append(f"[{inner.strip()}]({href})" if href and inner.strip() else inner)
        elif name in ("strong", "b"):
            out.append(f"**{inner.strip()}**" if inner.strip() else "")
        elif name in ("em", "i"):
            out.append(f"*{inner.strip()}*" if inner.strip() else "")
        elif name == "code":
            out.append(f"`{inner.strip()}`" if inner.strip() else "")
        elif name == "br":
            out.append("\n")
        else:
            out.append(inner)
    return "".join(out)


def _render(node, lines: list[str], base_url: str) -> None:
    from bs4 import NavigableString

    for child in node.children:
        if isinstance(child, NavigableString):
            text = re.sub(r"\s+", " ", str(child)).strip()
            if text:
                lines.extend([text, ""])
            continue
        name = child.name
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            heading = _inline(child, base_url).strip()
            if heading:
                lines.extend(["#" * level + " " + heading, ""])
        elif name == "p":
            text = _inline(child, base_url).strip()
            if text:
                lines.extend([text, ""])
        elif name in ("ul", "ol"):
            ordered = name == "ol"
            for i, li in enumerate(child.find_all("li", recursive=False), start=1):
                bullet = f"{i}." if ordered else "-"
                text = _inline(li, base_url).strip()
                if text:
                    lines.append(f"{bullet} {text}")
            lines.append("")
        elif name == "pre":
            code = child.get_text("\n").strip("\n")
            if code:
                lines.extend(["```", code, "```", ""])
        elif name == "blockquote":
            inner: list[str] = []
            _render(child, inner, base_url)
            lines.extend([f"> {ln}" if ln else ">" for ln in inner] + [""])
        elif name == "table":
            lines.extend(_render_table(child, base_url))
        elif name == "hr":
            lines.extend(["---", ""])
        elif name in ("dl", "div", "section", "body", "html", "span", "li", "figure",
                      "figcaption", "details", "summary", "td", "th", "tr", "tbody",
                      "thead", "main", "article"):
            _render(child, lines, base_url)
        else:
            text = _inline(child, base_url).strip()
            if text:
                lines.extend([text, ""])


def _render_table(table, base_url: str) -> list[str]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [_inline(td, base_url).strip().replace("|", r"\|")
                 for td in tr.find_all(["th", "td"], recursive=False)]
        if cells:
            rows.append(cells)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |",
           "| " + " | ".join(["---"] * width) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return out + [""]


# --------------------------------------------------------------------------
# local files
# --------------------------------------------------------------------------

def load_file(path: str | Path) -> Document:
    """Read one local document, dispatching on suffix."""
    p = Path(path)
    raw = p.read_bytes()
    suffix = p.suffix.lower()

    if suffix in PDF_SUFFIXES:
        title, text, media = p.stem, _pdf_text(p), "application/pdf"
        meta: dict = {}
    elif suffix in HTML_SUFFIXES:
        title, text, meta = html_to_markdown(raw.decode("utf-8", "replace"))
        media = "text/html"
        title = title or p.stem
    else:
        body = raw.decode("utf-8-sig", "replace")
        # a source doc may itself be markdown with frontmatter; keep the body
        from .parser import split_frontmatter
        fm, stripped = split_frontmatter(body)
        meta = {str(k): str(v) for k, v in fm.items()} if fm else {}
        text = _normalize_headings(_setext_to_atx(stripped.strip()))
        title = str(fm.get("title") or "") or _first_heading(text) or p.stem
        media = "text/markdown" if suffix in (".md", ".markdown") else "text/plain"

    stat = p.stat()
    return Document(
        uid=f"file:{p.resolve()}",
        origin="file",
        location=p.as_posix(),
        title=title.strip() or p.stem,
        text=text,
        slug=slugify(title or p.stem, p.stem),
        media_type=media,
        author=meta.get("author") or meta.get("dc.creator"),
        last_modified=(_meta_date(meta)
                       or _dt.date.fromtimestamp(stat.st_mtime).isoformat()),
        retrieved_at=_now(),
        sha256=_hash(raw),
        byte_len=len(raw),
    )


def _first_heading(text: str) -> Optional[str]:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def body_without_title(doc: "Document") -> str:
    """The document text minus its own title heading, headings re-levelled.

    A document's `# Title` becomes the concept's `title:`, so leaving it in the
    body would produce one giant section with the real structure buried at
    `##`. Dropping it and re-normalizing promotes the document's own sections
    to `#`, which is what the parser chunks on (SPEC §4.2).
    """
    text = re.sub(r"\A#\s+[^\n]*\n+", "", doc.text.lstrip(), count=1)
    return _normalize_headings(text.strip())


_SETEXT_RE = re.compile(r"^(?P<title>\S[^\n]*)\n(?P<rule>=+|-{2,})[ \t]*$", re.MULTILINE)


def _setext_to_atx(text: str) -> str:
    """Turn README-style underlined headings into `#`/`##`.

    Plain-text READMEs are a large slice of real corpora and they carry real
    structure — losing it would collapse a whole file into one section.
    """
    return _SETEXT_RE.sub(
        lambda m: ("# " if m.group("rule")[0] == "=" else "## ") + m.group("title").strip(),
        text,
    )


_META_DATE_KEYS = ("article:modified_time", "last_modified", "last-modified",
                   "date", "dc.date", "og:updated_time")


def _meta_date(meta: dict) -> Optional[str]:
    for key in _META_DATE_KEYS:
        value = _coerce_date(meta.get(key))
        if value:
            return value
    return None


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:                     # pragma: no cover - optional dep
        raise FetchError(
            "PDF support needs pypdf — install it with `uv sync --extra pdf`") from exc
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    return "\n\n".join(p for p in pages if p)


def load_paths(paths: Iterable[str | Path],
               suffixes: Optional[set[str]] = None) -> list[Document]:
    """Load files and/or recurse directories, skipping dotfiles."""
    suffixes = suffixes or SUPPORTED_SUFFIXES
    out: list[Document] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            candidates = sorted(
                f for f in p.rglob("*")
                if f.is_file() and f.suffix.lower() in suffixes
                and not any(part.startswith(".") for part in f.relative_to(p).parts)
            )
        elif p.is_file():
            candidates = [p]
        else:
            raise FileNotFoundError(f"no such path: {p}")
        out.extend(load_file(f) for f in candidates)
    return out


# --------------------------------------------------------------------------
# the web
# --------------------------------------------------------------------------

@dataclass
class FetchPolicy:
    """Deny-by-default rules for outbound requests."""

    timeout: float = 15.0
    max_bytes: int = 4 * 1024 * 1024
    max_redirects: int = 5
    user_agent: str = DEFAULT_USER_AGENT
    obey_robots: bool = True
    allow_private_hosts: bool = False     # opt-in for intranet/localhost testing
    delay: float = 0.5                    # politeness pause between requests
    allowed_schemes: tuple[str, ...] = ("http", "https")
    allowed_ports: tuple[int, ...] = (80, 443)


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    """Reject anything that is not routable public address space.

    `::ffff:127.0.0.1` is unwrapped first: an IPv4-mapped IPv6 literal is a
    standard way to smuggle a loopback address past a naive check.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _check_host(url: str, policy: FetchPolicy) -> None:
    """Refuse non-http(s) schemes, odd ports, and local network addresses.

    Applied to every redirect hop as well as the original URL — a public host
    that 302s to 169.254.169.254 is the whole point of an SSRF probe. The port
    allowlist matters as much as the address check: `http://127.0.0.1:8888/api/
    sessions` would otherwise write a live Jupyter token into a wiki concept.

    Residual risk, stated because it is not fixed here: this resolves the name
    and then lets the HTTP client resolve it again, so a DNS answer that
    changes between the two calls (rebinding) is not caught. Fetch untrusted
    URLs from somewhere that cannot reach anything you care about.
    """
    parsed = urlparse(url)
    if parsed.scheme not in policy.allowed_schemes:
        raise FetchError(f"scheme not allowed: {url!r}")
    host = parsed.hostname
    if not host:
        raise FetchError(f"no host in URL: {url!r}")
    if parsed.username or parsed.password:
        raise FetchError(f"credentials in URL are not accepted: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if policy.allow_private_hosts:
        return
    if port not in policy.allowed_ports:
        raise FetchError(
            f"port {port} not in {policy.allowed_ports} — pass "
            "allow_private_hosts=True to fetch from a non-standard port")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve {host!r}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public(ip):
            raise FetchError(
                f"{host!r} resolves to non-public address {ip} — pass "
                "allow_private_hosts=True if that is intentional")


_ROBOTS_CACHE: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}


def _robots_allows(url: str, policy: FetchPolicy, client) -> bool:
    if not policy.obey_robots:
        return True
    parsed = urlparse(url)
    origin = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    if origin not in _ROBOTS_CACHE:
        parser = urllib.robotparser.RobotFileParser()
        try:
            resp = client.get(origin, timeout=policy.timeout,
                              headers={"User-Agent": policy.user_agent})
            if resp.status_code >= 400:
                parser = None              # no robots.txt == no restrictions
            else:
                parser.parse(resp.text.splitlines())
        except Exception:
            parser = None
        _ROBOTS_CACHE[origin] = parser
    parser = _ROBOTS_CACHE[origin]
    return True if parser is None else parser.can_fetch(policy.user_agent, url)


def fetch(url: str, policy: Optional[FetchPolicy] = None, client=None) -> Document:
    """Fetch one URL into a `Document`, enforcing `policy` at every hop."""
    import httpx

    policy = policy or FetchPolicy()
    owns_client = client is None
    client = client or httpx.Client(follow_redirects=False)
    try:
        current = url
        for _ in range(policy.max_redirects + 1):
            _check_host(current, policy)
            if not _robots_allows(current, policy, client):
                raise FetchError(f"robots.txt disallows {current}")
            with client.stream("GET", current, timeout=policy.timeout,
                               headers={"User-Agent": policy.user_agent,
                                        "Accept": "text/html,text/plain,"
                                                  "application/pdf;q=0.8,*/*;q=0.5"}) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise FetchError(f"redirect without Location: {current}")
                    current = urljoin(current, location)
                    continue
                resp.raise_for_status()
                chunks, size = [], 0
                # `iter_bytes` yields *decoded* bytes, so the cap survives a
                # gzip bomb that advertises a small Content-Length.
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > policy.max_bytes:
                        raise FetchError(
                            f"{current} exceeds max_bytes={policy.max_bytes}")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return _document_from_response(current, raw, resp.headers)
        raise FetchError(f"too many redirects starting at {url}")
    finally:
        if owns_client:
            client.close()


def _document_from_response(url: str, raw: bytes, headers) -> Document:
    content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
    links: list[str] = []

    if content_type == "application/pdf" or url.lower().endswith(".pdf"):
        import io
        try:
            from pypdf import PdfReader
        except ImportError as exc:                 # pragma: no cover - optional dep
            raise FetchError("PDF support needs pypdf (`uv sync --extra pdf`)") from exc
        reader = PdfReader(io.BytesIO(raw))
        text = "\n\n".join(
            t for t in ((p.extract_text() or "").strip() for p in reader.pages) if t)
        title, meta = urlparse(url).path.rsplit("/", 1)[-1] or url, {}
    elif content_type in ("text/html", "application/xhtml+xml") or not content_type:
        title, text, meta = html_to_markdown(raw.decode("utf-8", "replace"), base_url=url)
        links = re.findall(r"\]\((https?://[^)\s]+)\)", text)
    else:
        title, text, meta = "", _normalize_headings(
            raw.decode("utf-8", "replace").strip()), {}
        title = _first_heading(text) or ""

    last_modified = _meta_date(meta) or headers.get("last-modified")
    return Document(
        uid=f"web:{url}",
        origin="web",
        location=url,
        title=(title or url).strip(),
        text=text,
        slug=slugify(title or urlparse(url).path, slugify(urlparse(url).netloc, "page")),
        media_type=content_type or "text/html",
        author=meta.get("author") or meta.get("article:author"),
        last_modified=_coerce_date(last_modified),
        retrieved_at=_now(),
        sha256=_hash(raw),
        byte_len=len(raw),
        links=links,
    )


def _coerce_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        pass
    try:                                    # RFC 7231, e.g. Last-Modified header
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return None


def crawl(seeds: Iterable[str], max_pages: int = 10,
          policy: Optional[FetchPolicy] = None, same_domain: bool = True,
          include: Optional[str] = None,
          exclude: Optional[str] = None) -> list[Document]:
    """Breadth-first crawl from `seeds`, bounded by `max_pages`.

    Deliberately shallow and same-domain by default: this exists to pull a
    handful of related pages, not to mirror a site.
    """
    import httpx

    policy = policy or FetchPolicy()
    inc = re.compile(include) if include else None
    exc = re.compile(exclude) if exclude else None
    seeds = list(seeds)
    domains = {urlparse(s).netloc for s in seeds}

    seen: set[str] = set()
    queue: list[str] = list(seeds)
    docs: list[Document] = []

    with httpx.Client(follow_redirects=False) as client:
        while queue and len(docs) < max_pages:
            url = queue.pop(0)
            key = url.split("#", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            try:
                doc = fetch(key, policy, client=client)
            except Exception as exc_:                # one bad page must not stop a crawl
                print(f"  skip {key}: {type(exc_).__name__}: {exc_}")
                continue
            docs.append(doc)
            for link in doc.links:
                target = link.split("#", 1)[0]
                if target in seen:
                    continue
                if same_domain and urlparse(target).netloc not in domains:
                    continue
                if inc and not inc.search(target):
                    continue
                if exc and exc.search(target):
                    continue
                queue.append(target)
            if policy.delay:
                time.sleep(policy.delay)
    return docs
