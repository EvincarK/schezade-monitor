from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

TARGETS = {
    "display": "https://www.schezade.co.kr/pagegen/v2/custom/display/index.php",
    "step_sale": "https://www.schezade.co.kr/pagegen/v2/custom/step-sale/index.php",
}

SITE_ROOT = "https://www.schezade.co.kr/"
OUT = Path("snapshots")
TIMEOUT = 30
SUPABASE_TABLE_SCHEZADE = "schezade_auction"


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


def parse_money(value: str | None) -> int | None:
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def query_int(url: str, key: str) -> int | None:
    values = parse_qs(urlsplit(url).query).get(key)
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


def element_text(node, selector: str) -> str | None:
    el = node.select_one(selector)
    if not el:
        return None
    text = normalize_text(el.get_text(" ", strip=True))
    return text or None


def parse_display_items(soup: BeautifulSoup) -> list[dict]:
    items_by_id: dict[int, dict] = {}

    for card in soup.select("article.card"):
        name_el = card.select_one("a.card__name[href*='/board/old_goods/g_detail2.html']")
        if not name_el:
            continue

        href = urljoin(SITE_ROOT, name_el.get("href", ""))
        listing_id = query_int(href, "no")
        if listing_id is None:
            continue

        current_price = parse_money(card.get("data-now"))
        if current_price is None:
            current_price = parse_money(element_text(card, ".price-sale"))

        discount_percent = parse_money(card.get("data-pct"))
        if discount_percent is None:
            discount_percent = parse_money(element_text(card, ".price-pct"))

        item = {
            "listing_id": listing_id,
            "product_gid": parse_money(card.get("data-pg-product-gid")),
            "brand": element_text(card, ".card__brand"),
            "product_name": normalize_text(name_el.get_text(" ", strip=True)),
            "category": card.get("data-cat") or element_text(card, ".card__meta"),
            "grade": card.get("data-grade") or element_text(card, ".badge"),
            "current_price": current_price,
            "original_price": parse_money(element_text(card, ".price-was")),
            "discount_percent": discount_percent,
            "condition": element_text(card, ".outlet-stock"),
            "url": href,
        }
        items_by_id[listing_id] = item

    return sorted(items_by_id.values(), key=lambda x: x["listing_id"], reverse=True)


def parse_step_sale_items(soup: BeautifulSoup) -> list[dict]:
    items_by_id: dict[int, dict] = {}

    # ss-card is the canonical desktop grid card. The page also contains
    # list/mobile representations of the same products, so only this class is parsed.
    for card in soup.select("article.ss-card"):
        hit = card.select_one("a.ss-hit[href*='/rauction/g_detail.html']")
        if not hit:
            continue

        href = urljoin(SITE_ROOT, hit.get("href", ""))
        sale_id = query_int(href, "ano")
        if sale_id is None:
            continue

        specs: dict[str, str] = {}
        for row in card.select(".ss-spec__row"):
            key = element_text(row, ".k")
            value = element_text(row, ".v")
            if key and value:
                specs[key] = value

        current_price = parse_money(card.get("data-ss-cur"))
        if current_price is None:
            current_price = parse_money(element_text(card, ".ss-now .amt"))

        item = {
            "sale_id": sale_id,
            "brand": element_text(card, ".ss-card__brand"),
            "product_name": element_text(card, ".ss-card__name"),
            "current_price": current_price,
            "discount_percent": parse_money(card.get("data-ss-rate")),
            "start_at": specs.get("시작일"),
            "discount_interval": specs.get("할인 주기"),
            "discount_amount": parse_money(specs.get("할인 단위")),
            "start_price": parse_money(specs.get("시작가")),
            "url": href,
        }
        items_by_id[sale_id] = item

    return sorted(items_by_id.values(), key=lambda x: x["sale_id"], reverse=True)


def load_previous_items(name: str) -> list[dict]:
    path = OUT / f"{name}_items.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("items", [])
        return items if isinstance(items, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def supabase_config() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL or SUPABASE_KEY is missing")
    return url, key


def verify_supabase() -> None:
    url, key = supabase_config()
    endpoint = f"{url}/rest/v1/{SUPABASE_TABLE_SCHEZADE}"
    r = requests.get(
        endpoint,
        params={"select": "id", "limit": "1"},
        headers={"apikey": key, "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()


def record_ended_schezade_sales(previous_items: list[dict], current_items: list[dict]) -> int:
    # Always verify the DB connection before accepting a new step-sale snapshot.
    # If Supabase is unavailable, the old snapshot is preserved so an ended auction
    # cannot be silently lost on the next run.
    verify_supabase()

    previous_by_id = {
        item.get("sale_id"): item
        for item in previous_items
        if isinstance(item.get("sale_id"), int)
    }
    current_ids = {
        item.get("sale_id")
        for item in current_items
        if isinstance(item.get("sale_id"), int)
    }
    ended_ids = sorted(set(previous_by_id) - current_ids)
    if not ended_ids:
        return 0

    rows = []
    for sale_id in ended_ids:
        item = previous_by_id[sale_id]
        item_name = item.get("product_name")
        bid_price = item.get("current_price")
        if not item_name or not isinstance(bid_price, int):
            raise RuntimeError(f"ended auction {sale_id} is missing name or final observed price")
        rows.append({"id": sale_id, "item_name": item_name, "bid_price": bid_price})

    url, key = supabase_config()
    endpoint = f"{url}/rest/v1/{SUPABASE_TABLE_SCHEZADE}"
    r = requests.post(
        endpoint,
        params={"on_conflict": "id"},
        headers={
            "apikey": key,
            "Content-Type": "application/json",
            "Prefer": "resolution=ignore-duplicates,return=minimal",
        },
        json=rows,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    print(f"Recorded {len(rows)} ended Schezade auction(s) in Supabase")
    return len(rows)


def write_structured_snapshot(name: str, meta: dict, items: list[dict]) -> int:
    payload = {
        "meta": meta,
        "count": len(items),
        "items": items,
    }
    (OUT / f"{name}_items.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(items)


def fetch_one(name: str, url: str, now: datetime) -> dict:
    previous_items = load_previous_items(name)

    stamp = int(now.timestamp())
    request_url = cache_busted(url, stamp)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; schezade-monitor/1.2; +https://github.com/EvincarK/schezade-monitor)",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    }

    r = requests.get(request_url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()

    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"

    html = r.text
    if len(html) < 1000:
        raise RuntimeError(f"{name}: response is suspiciously small ({len(html)} chars)")

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page_text = normalize_text(soup.get_text("\n", strip=True))

    if name == "display":
        items = parse_display_items(soup)
    elif name == "step_sale":
        items = parse_step_sale_items(soup)
        if not items:
            raise RuntimeError("step_sale: parser returned zero items")
        record_ended_schezade_sales(previous_items, items)
    else:
        items = []

    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = normalize_text(a.get_text(" ", strip=True))
        link_key = (href, text)
        if href and link_key not in seen:
            seen.add(link_key)
            links.append({"text": text, "href": href})

    sha256 = hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest()
    meta = {
        "name": name,
        "ok": True,
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

    item_count = write_structured_snapshot(name, meta, items)
    meta["item_count"] = item_count
    return meta


def main() -> None:
    now = datetime.now(timezone.utc)
    results = {}

    for name, url in TARGETS.items():
        try:
            results[name] = fetch_one(name, url, now)
        except Exception as exc:
            # Do not overwrite that target's last good HTML/JSON/items snapshot on failure.
            results[name] = {
                "name": name,
                "ok": False,
                "source_url": url,
                "fetched_at_utc": now.isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }

    status = {
        "fetched_at_utc": now.isoformat(),
        "ok": all(item.get("ok") for item in results.values()),
        "targets": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
