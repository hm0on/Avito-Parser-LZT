"""Website scanner for extracting contacts and legal identifiers.

Scans company website pages and linked info pages (about/contacts/requisites),
extracts phones, emails, INN/OGRN from text, and optionally applies OCR to images.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup
from src.config import settings
from src.proxy import iter_proxy_urls, website_proxy_manager

log = structlog.get_logger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_PHONE_RE = re.compile(r"(?:\+7|8)\D*\d{3}\D*\d{3}\D*\d{2}\D*\d{2}")
_EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>'\"()]+", re.IGNORECASE)
_INN_RE = re.compile(r"(?:инн|inn)\D{0,30}(\d{10}|\d{12})", re.IGNORECASE)
_OGRN_RE = re.compile(r"(?:огрнип|огрнип|огрн|ogrn)\D{0,30}(\d{13}|\d{15})", re.IGNORECASE)

_PRIORITY_LINK_TOKENS = (
    "about", "about-us", "company", "contacts", "contact", "info",
    "rekviz", "requisites", "legal", "documents", "docs",
    "o-kompanii", "o_nas", "onas", "kontakty", "rekvizity",
)

# Platform/service domains that should not be treated as contractor websites.
_NON_COMPANY_HOST_SUFFIXES = (
    "yandex.ru",
    "ya.ru",
    "yandex.net",
    "mds.yandex.net",
    "static-pano.maps.yandex.ru",
    "avito.ru",
    "2gis.ru",
    "vk.com",
    "vk.ru",
    "t.me",
    "telegram.me",
    "youtube.com",
    "youtu.be",
)


@dataclass
class WebsiteScanResult:
    website: str
    pages_scanned: int = 0
    pages: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    inn: str | None = None
    ogrn: str | None = None
    inn_found: bool = False
    ogrn_found: bool = False
    ocr_attempted: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "website": self.website,
            "pages_scanned": self.pages_scanned,
            "pages": self.pages,
            "phones": self.phones,
            "emails": self.emails,
            "inn": self.inn or "not_found",
            "ogrn": self.ogrn or "not_found",
            "inn_found": self.inn_found,
            "ogrn_found": self.ogrn_found,
            "ocr_attempted": self.ocr_attempted,
            "notes": self.notes,
        }


class WebsiteScanner:
    async def scan(self, website: str) -> WebsiteScanResult:
        base = _normalize_website(website)
        result = WebsiteScanResult(website=base)
        max_pages = max(1, settings.website_scan_max_pages)
        max_img = max(0, settings.website_scan_max_images)
        max_pdf = max(0, settings.website_scan_max_pdfs)
        timeout = max(4.0, settings.website_scan_timeout_seconds)

        proxy_candidates: list[str | None] = []
        if website_proxy_manager.count > 0:
            proxy_candidates.extend(
                [
                    _rewrite_proxy_scheme(proxy_url, settings.website_proxy_scheme)
                    for proxy_url in iter_proxy_urls(website_proxy_manager, purpose="website scanner")
                ]
            )
        # Final direct attempt avoids hard failure when proxy auth returns 407.
        proxy_candidates.append(None)

        for attempt_index, proxy_url in enumerate(proxy_candidates, start=1):
            q: list[str] = [base]
            seen: set[str] = set()
            img_links: list[str] = []
            pdf_links: list[str] = []
            attempt_result = WebsiteScanResult(website=base)
            proxy_mode = "direct" if proxy_url is None else "proxy"

            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": _UA},
                proxy=proxy_url,
            ) as client:
                while q and len(seen) < max_pages:
                    url = q.pop(0)
                    key = _url_key(url)
                    if key in seen:
                        continue
                    seen.add(key)

                    html = await _fetch_html(client, url)
                    if not html:
                        continue
                    attempt_result.pages.append(url)
                    attempt_result.pages_scanned += 1

                    soup = BeautifulSoup(html, "html.parser")
                    page_text = soup.get_text(" ", strip=True)
                    self._merge_extracted(attempt_result, page_text)

                    same_domain_links = _extract_internal_links(soup, url, base)
                    priority = [u for u in same_domain_links if _is_priority_link(u)]
                    ordinary = [u for u in same_domain_links if u not in priority]
                    for nxt in priority + ordinary:
                        if _url_key(nxt) not in seen and nxt not in q and len(seen) + len(q) < max_pages:
                            q.append(nxt)

                    img_links.extend(_extract_image_links(soup, url))
                    pdf_links.extend(_extract_pdf_links(soup, url))

                for pdf in _dedup(pdf_links)[:max_pdf]:
                    txt = await _fetch_pdf_text(client, pdf)
                    if txt:
                        self._merge_extracted(attempt_result, txt)

                if settings.website_scan_enable_ocr and img_links:
                    attempt_result.ocr_attempted = True
                    for img in _dedup(img_links)[:max_img]:
                        txt = await _ocr_image(client, img)
                        if txt:
                            self._merge_extracted(attempt_result, txt)

            if attempt_result.pages_scanned > 0:
                log.info(
                    "website_scanner.scan.ok",
                    website=base,
                    proxy_mode=proxy_mode,
                    attempt_count=attempt_index,
                    pages_scanned=attempt_result.pages_scanned,
                )
                return _finalize_result(attempt_result)

            fail_reason = "empty_or_blocked"
            if proxy_url:
                fail_reason = "proxy_empty_or_blocked"
            log.warning(
                "website_scanner.scan.retry",
                website=base,
                proxy_mode=proxy_mode,
                attempt_count=attempt_index,
                proxy_fail_reason=fail_reason,
            )
            result.notes.append(
                f"scan attempt failed via {'direct' if proxy_url is None else f'proxy {proxy_url}'}"
            )

        return _finalize_result(result)

    def _merge_extracted(self, result: WebsiteScanResult, text: str) -> None:
        for p in _extract_phones(text):
            if p not in result.phones:
                result.phones.append(p)
        for e in _extract_emails(text):
            if e not in result.emails:
                result.emails.append(e)
        if not result.inn:
            inn = _extract_inn(text)
            if inn:
                result.inn = inn
                result.inn_found = True
        if not result.ogrn:
            ogrn = _extract_ogrn(text)
            if ogrn:
                result.ogrn = ogrn
                result.ogrn_found = True


def _rewrite_proxy_scheme(proxy_url: str | None, scheme: str | None) -> str | None:
    if not proxy_url or not scheme:
        return proxy_url
    parsed = urlparse(proxy_url)
    if not parsed.netloc:
        return proxy_url
    desired = scheme.strip().lower()
    if (parsed.scheme or "").lower() == desired:
        return proxy_url
    return parsed._replace(scheme=desired).geturl()


async def _fetch_html(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url)
        ct = resp.headers.get("content-type", "").lower()
        if resp.status_code >= 400 or "text/html" not in ct:
            return None
        return resp.text
    except Exception:
        return None


def _extract_internal_links(soup: BeautifulSoup, current_url: str, site_root: str) -> list[str]:
    out: list[str] = []
    root = urlparse(site_root).netloc.lower()
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        u = urljoin(current_url, href)
        pr = urlparse(u)
        if pr.scheme not in ("http", "https"):
            continue
        if pr.netloc.lower() != root:
            continue
        out.append(_strip_fragment(u))
    return _dedup(out)


def _extract_image_links(soup: BeautifulSoup, current_url: str) -> list[str]:
    out: list[str] = []
    for img in soup.select("img[src]"):
        src = img.get("src", "").strip()
        if not src:
            continue
        u = urljoin(current_url, src)
        if re.search(r"\.(png|jpg|jpeg|webp)(\?.*)?$", u, re.IGNORECASE):
            out.append(_strip_fragment(u))
    return out


def _extract_pdf_links(soup: BeautifulSoup, current_url: str) -> list[str]:
    out: list[str] = []
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        u = urljoin(current_url, href)
        if re.search(r"\.pdf(\?.*)?$", u, re.IGNORECASE):
            out.append(_strip_fragment(u))
    return out


async def _fetch_pdf_text(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url)
        if resp.status_code >= 400:
            return None
        data = resp.content
        try:
            from pypdf import PdfReader  # optional dependency
        except Exception:
            return None
        from io import BytesIO

        reader = PdfReader(BytesIO(data))
        text_parts: list[str] = []
        for page in reader.pages[:10]:
            txt = page.extract_text() or ""
            if txt:
                text_parts.append(txt)
        return "\n".join(text_parts).strip() or None
    except Exception:
        return None


async def _ocr_image(client: httpx.AsyncClient, url: str) -> str | None:
    try:
        resp = await client.get(url)
        if resp.status_code >= 400:
            return None
        img_bytes = resp.content
        return await asyncio.to_thread(_ocr_bytes_sync, img_bytes)
    except Exception:
        return None


def _ocr_bytes_sync(img_bytes: bytes) -> str | None:
    try:
        import pytesseract  # optional dependency
        from PIL import Image, ImageOps  # optional dependency
        from io import BytesIO
    except Exception:
        return None

    try:
        img = Image.open(BytesIO(img_bytes)).convert("L")
        img = ImageOps.autocontrast(img)
        txt = pytesseract.image_to_string(img, lang="rus+eng")
        return txt.strip() or None
    except Exception:
        return None


def _extract_phones(text: str) -> list[str]:
    out: list[str] = []
    for m in _PHONE_RE.findall(text):
        p = re.sub(r"[^\d+]", "", m)
        if p.startswith("8") and len(p) == 11:
            p = "+7" + p[1:]
        if p and sum(ch.isdigit() for ch in p) >= 10 and p not in out:
            out.append(p)
    return out


def _extract_emails(text: str) -> list[str]:
    return _dedup([m.strip().lower() for m in _EMAIL_RE.findall(text)])


def _extract_inn(text: str) -> str | None:
    # Prefer INN with explicit marker
    m = _INN_RE.search(text)
    if m:
        cand = m.group(1)
        if _is_valid_inn(cand):
            return cand
    # Fallback: any valid-looking 10/12-digit number if marker missing
    for cand in re.findall(r"\b\d{10}\b|\b\d{12}\b", text):
        if _is_valid_inn(cand):
            return cand
    return None


def _extract_ogrn(text: str) -> str | None:
    m = _OGRN_RE.search(text)
    if m:
        cand = m.group(1)
        if _is_valid_ogrn(cand):
            return cand
    for cand in re.findall(r"\b\d{13}\b|\b\d{15}\b", text):
        if _is_valid_ogrn(cand):
            return cand
    return None


def _is_valid_inn(value: str) -> bool:
    if not value.isdigit() or len(value) not in (10, 12):
        return False
    d = [int(x) for x in value]
    if len(d) == 10:
        c = (2 * d[0] + 4 * d[1] + 10 * d[2] + 3 * d[3] + 5 * d[4] + 9 * d[5] + 4 * d[6] + 6 * d[7] + 8 * d[8]) % 11 % 10
        return c == d[9]
    c11 = (7 * d[0] + 2 * d[1] + 4 * d[2] + 10 * d[3] + 3 * d[4] + 5 * d[5] + 9 * d[6] + 4 * d[7] + 6 * d[8] + 8 * d[9]) % 11 % 10
    c12 = (3 * d[0] + 7 * d[1] + 2 * d[2] + 4 * d[3] + 10 * d[4] + 3 * d[5] + 5 * d[6] + 9 * d[7] + 4 * d[8] + 6 * d[9] + 8 * d[10]) % 11 % 10
    return c11 == d[10] and c12 == d[11]


def _is_valid_ogrn(value: str) -> bool:
    if not value.isdigit():
        return False
    if len(value) == 13:
        return int(value[:-1]) % 11 % 10 == int(value[-1])
    if len(value) == 15:
        return int(value[:-1]) % 13 % 10 == int(value[-1])
    return False


def _is_priority_link(url: str) -> bool:
    low = url.lower()
    return any(tok in low for tok in _PRIORITY_LINK_TOKENS)


def _normalize_website(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = "https://" + url
    pr = urlparse(url)
    path = pr.path or "/"
    return f"{pr.scheme}://{pr.netloc}{path}"


def _url_key(url: str) -> str:
    pr = urlparse(url)
    return f"{pr.scheme}://{pr.netloc}{pr.path.rstrip('/')}".lower()


def _strip_fragment(url: str) -> str:
    pr = urlparse(url)
    return f"{pr.scheme}://{pr.netloc}{pr.path}" + (f"?{pr.query}" if pr.query else "")


def _dedup(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _finalize_result(result: WebsiteScanResult) -> WebsiteScanResult:
    phones = _dedup(result.phones)
    # Drop obvious false positives derived from tax IDs.
    if result.ogrn:
        tail = result.ogrn[-10:]
        phones = [p for p in phones if not _digits(p).endswith(tail)]
    if result.inn:
        tail = result.inn[-10:]
        phones = [p for p in phones if not _digits(p).endswith(tail)]
    result.phones = phones[:30]
    result.emails = _dedup(result.emails)[:30]
    result.pages = _dedup(result.pages)[: max(1, int(settings.website_scan_max_pages))]
    if not result.inn:
        result.inn_found = False
    if not result.ogrn:
        result.ogrn_found = False
    return result


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def extract_website_candidates(raw) -> list[str]:
    """Extract website URLs from raw record fields."""
    cands: list[str] = []
    cj = raw.contacts_json or {}

    # structured contacts_json
    for key in ("websites", "website", "site", "url", "urls"):
        val = cj.get(key)
        if isinstance(val, str):
            cands.append(val)
        elif isinstance(val, list):
            for x in val:
                if isinstance(x, str):
                    cands.append(x)

    # any URLs in raw_payload JSON
    payload = raw.raw_payload
    if payload is not None:
        cands.extend(_extract_urls_deep(payload))

    # URLs from addresses/name text fragments
    for txt in [raw.name_raw] + (raw.addresses or []):
        if txt:
            cands.extend(_URL_RE.findall(txt))

    # Normalize and keep only likely company websites.
    norm: list[str] = []
    seen_hosts: set[str] = set()
    for x in cands:
        u = _normalize_website(x)
        if not u:
            continue
        parsed = urlparse(u)
        host = parsed.netloc.lower().split(":")[0]
        if not host:
            continue
        if not _is_likely_company_website(u):
            continue
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        norm.append(f"{parsed.scheme}://{parsed.netloc}/")
    return _dedup(norm)


def _extract_urls_deep(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(_extract_urls_deep(v))
    elif isinstance(obj, list):
        for it in obj:
            out.extend(_extract_urls_deep(it))
    elif isinstance(obj, str):
        out.extend(_URL_RE.findall(obj))
        # also plain domains in text
        out.extend(re.findall(r"\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s]*)?\b", obj))
    return out


def _is_likely_company_website(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if not host:
        return False

    for suffix in _NON_COMPANY_HOST_SUFFIXES:
        if host == suffix or host.endswith(f".{suffix}"):
            return False

    low = url.lower()
    if "/an/count/" in low:
        return False
    if "/get-altay/" in low or "/get-sprav-products/" in low:
        return False
    if "{size}" in low or "%s" in low:
        return False
    if "~2=" in low:  # tracking payload marker in yandex redirect URLs
        return False
    if len(low) > 500:
        return False
    return True
