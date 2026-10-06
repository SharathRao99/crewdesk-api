"""Read the contact links a candidate published on their own portfolio site.

Polite by design: honours robots.txt, fetches at most MAX_PAGES small HTML pages
per site, and does no JavaScript rendering (JS-only portfolios return nothing).
"""

import ipaddress
import logging
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "talent-sourcer/0.1 (portfolio contact lookup)"
MAX_PAGES = 4
MAX_BYTES = 1_500_000

LINKEDIN_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/(?:in|pub)/[^\s\"'<>?#)]+", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Only explicit international numbers (+CC ...) or tel: links, to avoid matching random digits.
PHONE_RE = re.compile(r"\+\d{1,3}[\s.-]?\(?\d{2,5}\)?(?:[\s.-]?\d{2,5}){1,3}(?!\d)")
RESUME_RE = re.compile(r"(?<![a-z])(resume|résumé|curriculum[ -]vitae|cv)(?![a-z])", re.I)
PAGE_RE = re.compile(r"(?<![a-z])(about|contact|resume|cv|hire)(?![a-z])", re.I)
FILE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".pdf", ".zip")
SKIP_HOSTS = {"github.com", "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com", "youtube.com"}


class _Links(HTMLParser):
    """Collects <a href> targets with their visible text."""

    def __init__(self) -> None:
        super().__init__()
        self.links: List[Tuple[str, str]] = []
        self._href: Optional[str] = None
        self._text: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href.strip(), " ".join("".join(self._text).split())))
            self._href = None


def clean_phone(raw: str) -> Optional[str]:
    """Normalise to '+<digits>' and keep only plausible lengths (8-15 digits)."""
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return None
    return ("+" if raw.strip().startswith("+") else "") + digits


def find_phones(text: str) -> Set[str]:
    return {p for p in (clean_phone(m) for m in PHONE_RE.findall(text)) if p}


def normalize_url(raw: Optional[str]) -> Optional[str]:
    """Return an http(s) URL worth crawling, or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not re.match(r"https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not host or "." not in host or host == "localhost":
        return None
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            return None
    except ValueError:
        pass
    if host.removeprefix("www.") in SKIP_HOSTS:
        return None
    return raw


def parse_page(html: str, base_url: str) -> Dict[str, Set[str]]:
    """Extract contact links from one HTML page."""
    parser = _Links()
    parser.feed(html)

    found: Dict[str, Set[str]] = {"linkedin": set(), "emails": set(), "resumes": set(), "pages": set(), "phones": set()}
    host = urlparse(base_url).hostname
    for href, text in parser.links:
        if href.lower().startswith("tel:"):
            phone = clean_phone(href[4:])
            if phone:
                found["phones"].add(phone)
            continue
        if not href or href.startswith(("#", "javascript:")):
            continue
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(email):
                found["emails"].add(email.lower())
            continue
        url = urljoin(base_url, href)
        if LINKEDIN_RE.match(url):
            found["linkedin"].add(url.split("?")[0].rstrip("/"))
        elif RESUME_RE.search(text) or RESUME_RE.search(urlparse(url).path):
            found["resumes"].add(url)
        elif urlparse(url).hostname == host and (PAGE_RE.search(text) or PAGE_RE.search(urlparse(url).path)):
            if not urlparse(url).path.lower().endswith(FILE_EXT):
                found["pages"].add(url.split("#")[0])

    for match in LINKEDIN_RE.findall(html):
        found["linkedin"].add(match.split("?")[0].rstrip("/"))
    visible = re.sub(r"<[^>]+>", " ", html)
    found["phones"] |= find_phones(visible)
    for match in EMAIL_RE.findall(visible):
        if not match.lower().endswith(FILE_EXT + (".woff", ".woff2")) and "@2x" not in match:
            found["emails"].add(match.lower())
    return found


def _allowed(client: httpx.Client, url: str, cache: Dict[str, RobotFileParser]) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in cache:
        robots = RobotFileParser()
        try:
            response = client.get(origin + "/robots.txt")
            robots.parse(response.text.splitlines() if response.status_code == 200 else [])
        except httpx.HTTPError:
            robots.parse([])
        cache[origin] = robots
    return cache[origin].can_fetch(USER_AGENT, url)


def _fetch(client: httpx.Client, url: str) -> Optional[str]:
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
                return None
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    break
            return body.decode(response.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as e:
        logger.debug(f"fetch failed {url}: {e}")
        return None


def crawl_portfolio(raw_url: Optional[str]) -> Dict[str, object]:
    """Crawl the homepage plus a few about/contact/resume pages."""
    url = normalize_url(raw_url)
    empty: Dict[str, object] = {"portfolio_url": url, "linkedin": None, "emails": [], "resumes": [], "phones": []}
    if not url:
        return empty

    totals: Dict[str, Set[str]] = {"linkedin": set(), "emails": set(), "resumes": set(), "phones": set()}
    queue, seen, robots = [url], set(), {}
    with httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        while queue and len(seen) < MAX_PAGES:
            page = queue.pop(0)
            if page in seen or not _allowed(client, page, robots):
                continue
            seen.add(page)
            html = _fetch(client, page)
            if html is None:
                continue
            found = parse_page(html, page)
            for key in totals:
                totals[key] |= found[key]
            queue += sorted(found["pages"] - seen)

    return {
        "portfolio_url": url,
        "linkedin": sorted(totals["linkedin"])[0] if totals["linkedin"] else None,
        "emails": sorted(totals["emails"])[:3],
        "resumes": sorted(totals["resumes"])[:3],
        "phones": sorted(totals["phones"])[:2],
    }
