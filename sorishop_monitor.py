from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

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
TABLE = "sorishop_auction"


def normalize(text: str) -> str:
    return "\n".join(
        line for line in (re.sub(r"\s+", " ", x).strip() for x in text.splitlines()) if line
    )


def money(text: str | None) -> int | None:
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def cache_busted(url: str, stamp: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_sorishop_monitor_ts"] = str(stamp)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def auction_id(href: str) -> int | None:
    match = re.search(r"(?:[?&]|&amp;)ano=(\d+)", href, re.I)
    return int(match.group(1)) if match else None


def card_specs(card: Tag) -> dict[str, str]:
    specs: dict[str, str] = {}
    for row in card.select(".gd-sp"):
        spans = row.find_all("span", recursive=False)
        if len(spans) < 2:
            continue
        key = normalize(spans[0].get_text(" ", strip=True))
        value = normalize(spans[1].get_text(" ", strip=True))
        if key and value:
            specs[key] = value
    return specs


def labeled_money(text: str, label: str) -> int | None:
    match = re.search(rf"{re.escape(label)}\s*[:：]?\s*([0-9][0-9,]*)\s*원", text)
    return money(match.group(1)) if match else None


def labeled_line(text: str, *labels: str) -> str | None:
    for label in labels:
        match = re.search(rf"{re.escape(label)}\s*[:：]?\s*([^\n]+)", text)
        if match:
            return match.group(1).strip()
    return None


def exposed_order_price(card: Tag) -> int | None:
    # A numeric paid/order price, if Sorishop leaves one in an attribute, wins over
    # the masked display text. Generic price attributes are intentionally ignored.
    for tag in [card, *card.find_all(True)]:
        for key, value in tag.attrs.items():
            key_l = str(key).lower().replace("-", "_")
            if "price" not in key_l or not any(word in key_l for word in ("order", "bid", "paid")):
                continue
            if isinstance(value, list):
                value = " ".join(map(str, value))
            parsed = money(str(value))
            if parsed is not None:
                return parsed

    # Also inspect inline script snippets contained by the card, but only keys whose
    # names explicitly mean order/bid/paid price.
    raw = str(card)
    patterns = (
        r"(?:order|bid|paid)[_-]?price\s*[:=]\s*['\"]?([0-9][0-9,]*)",
        r"price[_-]?(?:order|bid|paid)\s*[:=]\s*['\"]?([0-9][0-9,]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            return money(match.group(1))
    return None


def parse_items(soup: BeautifulSoup) -> tuple[list[dict], list[dict], dict]:
    active: dict[int, dict] = {}
    completed: dict[int, dict] = {}
    unresolved: list[int] = []
    cards = soup.select("article.gd-card")

    for card in cards:
        link = card.select_one("a.gd-overlay[href*='ano=']") or card.select_one("a[href*='ano=']")
        if not link:
            continue
        href_raw = str(link.get("href", ""))
        aid = auction_id(href_raw)
        if aid is None:
            continue

        name_el = card.select_one(".gd-name")
        name = normalize(name_el.get_text(" ", strip=True)) if name_el else None
        specs = card_specs(card)
        text = normalize(card.get_text("\n", strip=True))
        common = {
            "auction_id": aid,
            "item_name": name,
            "start_at": specs.get("시작 일시") or specs.get("시작일시"),
            "discount_interval": specs.get("할인 단위"),
            "discount_amount": money(specs.get("할인 단위")),
            "url": urljoin(SITE_ROOT, href_raw),
        }

        now_el = card.select_one(".gd-now")
        current_price = money(now_el.get_text(" ", strip=True)) if now_el else None
        if current_price is not None:
            active[aid] = {
                **common,
                "current_price": current_price,
                "original_price": money(card.select_one(".gd-was").get_text(" ", strip=True)) if card.select_one(".gd-was") else None,
                "discount_percent": money(card.select_one(".gd-rate").get_text(" ", strip=True)) if card.select_one(".gd-rate") else None,
            }
            continue

        if any(label in text for label in ("주문 시각", "주문시각", "주문가", "판매가")):
            order_price = labeled_money(text, "주문가")
            if order_price is None:
                order_price = exposed_order_price(card)
            completed[aid] = {
                **common,
                "order_at": labeled_line(text, "주문 시각", "주문시각"),
                "order_price": order_price,
                "order_price_masked": order_price is None,
            }
            continue

        unresolved.append(aid)

    diagnostics = {
        "card_count": len(cards),
        "active_count": len(active),
        "completed_count": len(completed),
        "unresolved_count": len(unresolved),
        "unresolved_ids_sample": sorted(unresolved, reverse=True)[:20],
    }
    return (
        sorted(active.values(), key=lambda x: x["auction_id"], reverse=True),
        sorted(completed.values(), key=lambda x: x["auction_id"], reverse=True),
        diagnostics,
    )


def previous_items() -> list[dict]:
    if not ITEMS_PATH.exists():
        return []
    try:
        payload = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
        items = payload.get("items", [])
        return items if isinstance(items, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def db_config() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL or SUPABASE_KEY is missing")
    return url, key


def verify_db() -> None:
    url, key = db_config()
    response = requests.get(
        f"{url}/rest/v1/{TABLE}",
        params={"select": "id", "limit": "1"},
        headers={"apikey": key, "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def record_ended(previous: list[dict], current: list[dict], completed: list[dict]) -> int:
    # DB connectivity is part of accepting a new good snapshot. If it fails, the
    # workflow fails before committing, so an ending event cannot be lost.
    verify_db()

    previous_by_id = {
        item.get("auction_id"): item
        for item in previous
        if isinstance(item.get("auction_id"), int)
    }
    current_ids = {
        item.get("auction_id")
        for item in current
        if isinstance(item.get("auction_id"), int)
    }
    completed_by_id = {
        item.get("auction_id"): item
        for item in completed
        if isinstance(item.get("auction_id"), int)
    }
    ended_ids = sorted(set(previous_by_id) - current_ids)
    if not ended_ids:
        return 0

    # A genuine disappearance must also be visible in the completed/order area.
    # Otherwise treat the response as partial/broken rather than a mass sale event.
    missing = [aid for aid in ended_ids if aid not in completed_by_id]
    if missing:
        raise RuntimeError(
            "ended IDs absent from completed list; refusing snapshot: "
            + ", ".join(map(str, missing[:20]))
        )

    rows: list[dict] = []
    for aid in ended_ids:
        old = previous_by_id[aid]
        sold = completed_by_id[aid]
        item_name = sold.get("item_name") or old.get("item_name")
        actual_order_price = sold.get("order_price")
        last_observed_price = old.get("current_price")
        final_price = actual_order_price if isinstance(actual_order_price, int) else last_observed_price
        if not item_name or not isinstance(final_price, int):
            raise RuntimeError(f"ended auction {aid} lacks name/final price")
        source = "actual_order_price" if isinstance(actual_order_price, int) else "last_observed_price"
        print(f"Ended Sorishop auction {aid}: {item_name} / {final_price} ({source})")
        rows.append({"id": aid, "item_name": item_name, "bid_price": final_price})

    url, key = db_config()
    response = requests.post(
        f"{url}/rest/v1/{TABLE}",
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
    previous = previous_items()
    request_url = cache_busted(TARGET_URL, int(now.timestamp()))

    try:
        response = requests.get(
            request_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
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
            raise RuntimeError(f"response suspiciously small: {len(html)} chars")

        soup = BeautifulSoup(html, "html.parser")
        page_text = normalize(soup.get_text("\n", strip=True))
        if "계단식 세일" not in page_text:
            raise RuntimeError("expected step-sale marker not found")

        active, completed, diagnostics = parse_items(soup)
        print("SORISHOP DIAGNOSTICS", json.dumps(diagnostics, ensure_ascii=False))
        if diagnostics["card_count"] == 0:
            raise RuntimeError("no gd-card auction cards parsed")
        if not active:
            raise RuntimeError("active parser returned zero items")
        if diagnostics["unresolved_count"] > max(3, diagnostics["card_count"] // 20):
            raise RuntimeError("too many unresolved Sorishop auction cards")

        recorded = record_ended(previous, active, completed)
        meta = {
            "name": "sorishop",
            "ok": True,
            "source_url": TARGET_URL,
            "request_url": request_url,
            "final_url": response.url,
            "fetched_at_utc": now.isoformat(),
            "status_code": response.status_code,
            "title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "html_chars": len(html),
            "text_chars": len(page_text),
            "sha256": hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
            "recorded_ended_count": recorded,
            **diagnostics,
        }

        # Replace good snapshots only after parsing and DB verification succeed.
        HTML_PATH.write_text(html, encoding="utf-8")
        PAGE_PATH.write_text(
            json.dumps({"meta": meta, "page_text": page_text}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        ITEMS_PATH.write_text(
            json.dumps(
                {
                    "meta": meta,
                    "count": len(active),
                    "items": active,
                    "completed_count": len(completed),
                    "completed_items": completed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        STATUS_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(meta, ensure_ascii=False, indent=2))

    except Exception as exc:
        # Keep prior good HTML/JSON/items untouched. Status is diagnostic only and is
        # not committed by the workflow when this process exits non-zero.
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
