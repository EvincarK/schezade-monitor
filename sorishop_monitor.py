from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

TARGET_URL = "https://www.sorishop.com/rauction/g_list.html"
SITE_ROOT = "https://www.sorishop.com/"
OUT = Path("snapshots")
ITEMS_PATH = OUT / "sorishop_items.json"
HTML_PATH = OUT / "sorishop.html"
PAGE_PATH = OUT / "sorishop.json"
STATUS_PATH = OUT / "sorishop_status.json"
TIMEOUT = 30
SUPABASE_TABLE = "sorishop_auction"


def cache_busted(url: str, stamp: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_sorishop_monitor_ts"] = str(stamp)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def normalize_text(text: str) -> str:
    lines: list[str] = []
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


def auction_id_from_href(href: str) -> int | None:
    absolute = urljoin(SITE_ROOT, href)
    if "/rauction/g_detail.html" not in urlsplit(absolute).path:
        return None
    return query_int(absolute, "ano")


def unique_auction_ids(node: Tag) -> set[int]:
    ids: set[int] = set()
    for link in node.find_all("a", href=True):
        auction_id = auction_id_from_href(link.get("href", ""))
        if auction_id is not None:
            ids.add(auction_id)
    return ids


def find_item_container(anchor: Tag, auction_id: int) -> Tag | None:
    """Find the smallest ancestor that looks like exactly one auction card.

    Sorishop has changed card class names over time, so the parser intentionally
    keys off stable user-visible labels and the detail URL instead of a CSS class.
    """
    node: Tag | None = anchor
    for _ in range(14):
        parent = node.parent if node else None
        if not isinstance(parent, Tag):
            break
        node = parent
        text = normalize_text(node.get_text("\n", strip=True))
        if not ("시작" in text and ("현재가" in text or "주문시각" in text or "주문가" in text)):
            continue
        ids = unique_auction_ids(node)
        if ids == {auction_id}:
            return node
    return None


def regex_money(text: str, label: str) -> int | None:
    # Examples: "현재가 1,234,000원", "주문가 850,000원".
    # Masked values such as "주문가 ****원" intentionally return None.
    m = re.search(rf"{re.escape(label)}\s*([0-9][0-9,]*)\s*원", text)
    return parse_money(m.group(1)) if m else None


def regex_value(text: str, label: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s*[:：]?\s*([^\n]+)", text)
    return m.group(1).strip() if m else None


def numeric_order_price_attr(node: Tag) -> int | None:
    """Use a numeric order/bid-price data attribute if Sorishop exposes one.

    This is deliberately conservative: generic data-price attributes are ignored
    because they can be the list/start/current price instead of the paid price.
    """
    for tag in [node, *node.find_all(True)]:
        for key, value in tag.attrs.items():
            key_l = str(key).lower().replace("-", "_")
            if not ("price" in key_l and ("order" in key_l or "bid" in key_l or "paid" in key_l)):
                continue
            if isinstance(value, list):
                value = " ".join(str(x) for x in value)
            parsed = parse_money(str(value))
            if parsed is not None:
                return parsed
    return None


def best_item_name(anchors: list[Tag]) -> str | None:
    candidates = [normalize_text(a.get_text(" ", strip=True)) for a in anchors]
    candidates = [x for x in candidates if x]
    return max(candidates, key=len) if candidates else None


def parse_auction_items(soup: BeautifulSoup) -> tuple[list[dict], list[dict], dict]:
    anchors_by_id: dict[int, list[Tag]] = {}
    for anchor in soup.find_all("a", href=True):
        auction_id = auction_id_from_href(anchor.get("href", ""))
        if auction_id is not None:
            anchors_by_id.setdefault(auction_id, []).append(anchor)

    active_by_id: dict[int, dict] = {}
    completed_by_id: dict[int, dict] = {}
    unresolved_ids: list[int] = []

    for auction_id, anchors in anchors_by_id.items():
        named_anchors = [a for a in anchors if normalize_text(a.get_text(" ", strip=True))]
        search_anchors = named_anchors + [a for a in anchors if a not in named_anchors]
        container = None
        for anchor in search_anchors:
            container = find_item_container(anchor, auction_id)
            if container is not None:
                break
        if container is None:
            unresolved_ids.append(auction_id)
            continue

        text = normalize_text(container.get_text("\n", strip=True))
        item_name = best_item_name(anchors)
        href = urljoin(SITE_ROOT, anchors[0].get("href", ""))

        current_price = regex_money(text, "현재가")
        order_price = regex_money(text, "주문가")
        if order_price is None and "주문가" in text:
            order_price = numeric_order_price_attr(container)

        common = {
            "auction_id": auction_id,
            "item_name": item_name,
            "start_at": regex_value(text, "시작 일시") or regex_value(text, "시작일시"),
            "discount_interval": regex_value(text, "할인"),
            "discount_amount": regex_money(text, "할인 단위"),
            "url": href,
        }

        if current_price is not None and "현재가" in text:
            active_by_id[auction_id] = {
                **common,
                "current_price": current_price,
            }
        elif "주문시각" in text or "주문가" in text:
            completed_by_id[auction_id] = {
                **common,
                "order_at": regex_value(text, "주문시각"),
                "order_price": order_price,
                "order_price_masked": order_price is None,
            }
        else:
            unresolved_ids.append(auction_id)

    diagnostics = {
        "detail_id_count": len(anchors_by_id),
        "active_count": len(active_by_id),
        "completed_count": len(completed_by_id),
        "unresolved_count": len(unresolved_ids),
        "unresolved_ids_sample": sorted(unresolved_ids, reverse=True)[:20],
    }
    active = sorted(active_by_id.values(), key=lambda x: x["auction_id"], reverse=True)
    completed = sorted(completed_by_id.values(), key=lambda x: x["auction_id"], reverse=True)
    return active, completed, diagnostics


def load_previous_items() -> list[dict]:
    if not ITEMS_PATH.exists():
        return []
    try:
        payload = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
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
    endpoint = f"{url}/rest/v1/{SUPABASE_TABLE}"
    response = requests.get(
        endpoint,
        params={"select": "id", "limit": "1"},
        headers={"apikey": key, "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def record_ended_sales(previous_items: list[dict], current_items: list[dict], completed_items: list[dict]) -> int:
    # Verify DB before accepting the new snapshot. If DB is unavailable, this run
    # fails and the workflow does not commit the new snapshot, preserving the event.
    verify_supabase()

    previous_by_id = {
        item.get("auction_id"): item
        for item in previous_items
        if isinstance(item.get("auction_id"), int)
    }
    current_ids = {
        item.get("auction_id")
        for item in current_items
        if isinstance(item.get("auction_id"), int)
    }
    completed_by_id = {
        item.get("auction_id"): item
        for item in completed_items
        if isinstance(item.get("auction_id"), int)
    }

    ended_ids = sorted(set(previous_by_id) - current_ids)
    if not ended_ids:
        return 0

    # A genuine hourly sale disappearance should immediately appear in Sorishop's
    # completed/order area. If it does not, treat the run as suspicious rather than
    # converting a partial parse/site outage into a mass sale event.
    missing_from_completed = [auction_id for auction_id in ended_ids if auction_id not in completed_by_id]
    if missing_from_completed:
        raise RuntimeError(
            "ended auction(s) missing from completed list; refusing snapshot: "
            + ", ".join(map(str, missing_from_completed[:20]))
        )

    rows: list[dict] = []
    for auction_id in ended_ids:
        previous = previous_by_id[auction_id]
        completed = completed_by_id[auction_id]
        item_name = completed.get("item_name") or previous.get("item_name")
        order_price = completed.get("order_price")
        last_observed_price = previous.get("current_price")
        bid_price = order_price if isinstance(order_price, int) else last_observed_price
        if not item_name or not isinstance(bid_price, int):
            raise RuntimeError(f"ended auction {auction_id} is missing name or usable final price")
        source = "actual_order_price" if isinstance(order_price, int) else "last_observed_price"
        print(f"Ended Sorishop auction {auction_id}: {item_name} / {bid_price} ({source})")
        rows.append({"id": auction_id, "item_name": item_name, "bid_price": bid_price})

    url, key = supabase_config()
    endpoint = f"{url}/rest/v1/{SUPABASE_TABLE}"
    response = requests.post(
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
    response.raise_for_status()
    print(f"Recorded {len(rows)} ended Sorishop auction(s) in Supabase")
    return len(rows)


def main() -> None:
    now = datetime.now(timezone.utc)
    OUT.mkdir(parents=True, exist_ok=True)
    previous_items = load_previous_items()
    request_url = cache_busted(TARGET_URL, int(now.timestamp()))

    try:
        response = requests.get(
            request_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; sorishop-monitor/1.0; +https://github.com/EvincarK/schezade-monitor)",
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
            },
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        response.raise_for_status()
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = response.apparent_encoding or "utf-8"

        html = response.text
        if len(html) < 10_000:
            raise RuntimeError(f"response is suspiciously small ({len(html)} chars)")

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        page_text = normalize_text(soup.get_text("\n", strip=True))
        if "계단식 세일" not in page_text:
            raise RuntimeError("expected Sorishop step-sale marker not found")

        active_items, completed_items, diagnostics = parse_auction_items(soup)
        if diagnostics["detail_id_count"] == 0:
            raise RuntimeError("no auction detail IDs were parsed")
        if not active_items and previous_items:
            raise RuntimeError("active parser returned zero items while previous snapshot is non-empty")

        recorded = record_ended_sales(previous_items, active_items, completed_items)

        meta = {
            "name": "sorishop",
            "ok": True,
            "source_url": TARGET_URL,
            "request_url": request_url,
            "final_url": response.url,
            "fetched_at_utc": now.isoformat(),
            "status_code": response.status_code,
            "title": title,
            "html_chars": len(html),
            "text_chars": len(page_text),
            "sha256": hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
            "recorded_ended_count": recorded,
            **diagnostics,
        }

        # Write only after all parsing and Supabase work succeeds.
        HTML_PATH.write_text(html, encoding="utf-8")
        PAGE_PATH.write_text(
            json.dumps(
                {"meta": meta, "page_text": page_text},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        ITEMS_PATH.write_text(
            json.dumps(
                {
                    "meta": meta,
                    "count": len(active_items),
                    "items": active_items,
                    "completed_count": len(completed_items),
                    "completed_items": completed_items,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        STATUS_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(meta, ensure_ascii=False, indent=2))

    except Exception as exc:
        # Keep the last good HTML/items snapshot untouched. Only status is updated.
        status = {
            "name": "sorishop",
            "ok": False,
            "source_url": TARGET_URL,
            "fetched_at_utc": now.isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(status, ensure_ascii=False, indent=2))
        raise


if __name__ == "__main__":
    main()
