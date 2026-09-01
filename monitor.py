from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

TARGETS = {
    "display": "https://www.schezade.co.kr/pagegen/v2/custom/display/index.php",
    "step_sale": "https://www.schezade.co.kr/pagegen/v2/custom/step-sale/index.php",
}

OUT = Path("snapshots")
TIMEOUT = 30


def cache_busted(url: str, stamp: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_schezade_monitor_ts"] = str(stamp)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def normalize_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def fetch_one(name: str, url: str, now: datetime) -> dict:
    stamp = int(now.timestamp())
    request_url = cache_busted(url, stamp)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; schezade-monitor/1.0; +https://github.com/EvincarK/schezade-monitor)",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    }

    r = requests.get(request_url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()

    # requests occasionally guesses legacy Korean encodings poorly.
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"

    html = r.text
    if len(html) < 1000:
        raise RuntimeError(f"{name}: response is suspiciously small ({len(html)} chars)")

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page_text = normalize_text(soup.get_text("\n", strip=True))

    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = normalize_text(a.get_text(" ", strip=True))
        key = (href, text)
        if href and key not in seen:
            seen.add(key)
            links.append({"text": text, "href": href})

    sha256 = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()
    meta = {
        "name": name,
        "source_url": url,
        "request_url": request_url,
        "final_url": r.url,
        "fetched_at_utc": now.isoformat(),
        "status_code": r.status_code,
        "title": title,
        "html_chars": len(html),
        "text_chars": len(page_text),
        "link_count": len(links),
        "sha256": sha256,
        "response_headers": {
            k: v
            for k, v in r.headers.items()
            if k.lower() in {"date", "age", "cache-control", "etag", "last-modified", "expires", "via", "x-cache"}
        },
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.html").write_text(html, encoding="utf-8")
    (OUT / f"{name}.json").write_text(
        json.dumps({"meta": meta, "page_text": page_text, "links": links}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return meta


def main() -> None:
    now = datetime.now(timezone.utc)
    results = {}
    errors = {}

    for name, url in TARGETS.items():
        try:
            results[name] = fetch_one(name, url, now)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"

    status = {
        "fetched_at_utc": now.isoformat(),
        "ok": not errors and len(results) == len(TARGETS),
        "targets": results,
        "errors": errors,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(status, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
