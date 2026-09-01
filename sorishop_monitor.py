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
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q["_sorishop_monitor_ts"] = str(stamp)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def auction_id(href: str) -> int | None:
    m = re.search(r"(?:[?&]|&amp;)ano=(\d+)", href, re.I)
    return int(m.group(1)) if m else None


def card_for(anchor: Tag) -> Tag | None:
    # Take the nearest ancestor containing the auction fields. This avoids scanning
    # every descendant of large page wrappers and keeps the parser O(number of cards).
    node: Tag | None = anchor
    for _ in range(12):
        parent = node.parent if node else None
        if not isinstance(parent, Tag):
            return None
        node = parent
        text = normalize(node.get_text("\n", strip=True))
        if "시작" in text and any(label in text for label in ("현재가", "주문 시각", "주문시각", "주문가")):
            return node
    return None


def labeled_money(text: str, label: str) -> int | None:
    m = re.search(rf"{re.escape(label)}\s*([0-9][0-9,]*)\s*원", text)
    return money(m.group(1)) if m else None


def labeled_line(text: str, *labels: str) -> str | None:
    for label in labels:
        m = re.search(rf"{re.escape(label)}\s*[:：]?\s*([^\n]+)", text)
        if m:
            return m.group(1).strip()
    return None


def exposed_order_price(node: Tag) -> int | None:
    for tag in [node, *node.find_all(True)]:
        for key, value in tag.attrs.items():
            k = str(key).lower().replace("-", "_")
            if "price" not in k or not any(word in k for word in ("order", "bid", "paid")):
                continue
            if isinstance(value, list):
                value = " ".join(map(str, value))
            parsed = money(str(value))
            if parsed is not None:
                return parsed
    return None


def parse_items(soup: BeautifulSoup) -> tuple[list[dict], list[dict], dict]:
    anchors: dict[int, list[Tag]] = {}
    all_hrefs: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", ""))
        all_hrefs.append(href)
        aid = auction_id(href)
        if aid is not None:
            anchors.setdefault(aid, []).append(a)

    active: dict[int, dict] = {}
    completed: dict[int, dict] = {}
    unresolved: list[int] = []

    for aid, links in anchors.items():
        card = None
        for a in sorted(links, key=lambda x: bool(normalize(x.get_text(" ", strip=True))), reverse=True):
            card = card_for(a)
            if card is not None:
                break
        if card is None:
            unresolved.append(aid)
            continue

        text = normalize(card.get_text("\n", strip=True))
        names = [normalize(a.get_text(" ", strip=True)) for a in links]
        names = [x for x in names if x]
        name = max(names, key=len) if names else None
        href = urljoin(SITE_ROOT, str(links[0].get("href", "")))
        common = {
            "auction_id": aid,
            "item_name": name,
            "start_at": labeled_line(text, "시작 일시", "시작일시"),
            "discount_amount": labeled_money(text, "할인 단위"),
            "url": href,
        }

        current = labeled_money(text, "현재가")
        if current is not None:
            active[aid] = {**common, "current_price": current}
            continue

        if any(label in text for label in ("주문 시각", "주문시각", "주문가")):
            order = labeled_money(text, "주문가")
            if order is None:
                order = exposed_order_price(card)
            completed[aid] = {
                **common,
                "order_at": labeled_line(text, "주문 시각", "주문시각"),
                "order_price": order,
                "order_price_masked": order is None,
            }
            continue

        unresolved.append(aid)

    diag = {
        "detail_id_count": len(anchors),
        "active_count": len(active),
        "completed_count": len(completed),
        "unresolved_count": len(unresolved),
        "unresolved_ids_sample": sorted(unresolved, reverse=True)[:20],
        "href_sample": [x for x in all_hrefs if "detail" in x.lower() or "ano" in x.lower()][:20],
    }
    return (
        sorted(active.values(), key=lambda x: x["auction_id"], reverse=True),
        sorted(completed.values(), key=lambda x: x["auction_id"], reverse=True),
        diag,
    )


def previous_items() -> list[dict]:
    if not ITEMS_PATH.exists():
        return []
    try:
        data = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
        return data.get("items", []) if isinstance(data.get("items", []), list) else []
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
    r = requests.get(
        f"{url}/rest/v1/{TABLE}",
        params={"select": "id", "limit": "1"},
        headers={"apikey": key, "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()


def record_ended(previous: list[dict], current: list[dict], completed: list[dict]) -> int:
    verify_db()
    prev = {x.get("auction_id"): x for x in previous if isinstance(x.get("auction_id"), int)}
    cur_ids = {x.get("auction_id") for x in current if isinstance(x.get("auction_id"), int)}
    done = {x.get("auction_id"): x for x in completed if isinstance(x.get("auction_id"), int)}
    ended = sorted(set(prev) - cur_ids)
    if not ended:
        return 0

    missing = [x for x in ended if x not in done]
    if missing:
        raise RuntimeError("ended IDs absent from completed list; refusing snapshot: " + ", ".join(map(str, missing[:20])))

    rows = []
    for aid in ended:
        old, sold = prev[aid], done[aid]
        name = sold.get("item_name") or old.get("item_name")
        actual = sold.get("order_price")
        observed = old.get("current_price")
        final = actual if isinstance(actual, int) else observed
        if not name or not isinstance(final, int):
            raise RuntimeError(f"ended auction {aid} lacks name/final price")
        source = "actual_order_price" if isinstance(actual, int) else "last_observed_price"
        print(f"Ended Sorishop auction {aid}: {name} / {final} ({source})")
        rows.append({"id": aid, "item_name": name, "bid_price": final})

    url, key = db_config()
    r = requests.post(
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
    r.raise_for_status()
    print(f"Recorded {len(rows)} ended Sorishop auction(s) in Supabase")
    return len(rows)


def main() -> None:
    now = datetime.now(timezone.utc)
    OUT.mkdir(parents=True, exist_ok=True)
    previous = previous_items()
    request_url = cache_busted(TARGET_URL, int(now.timestamp()))

    try:
        r = requests.get(
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
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        html = r.text
        if len(html) < 10_000:
            raise RuntimeError(f"response suspiciously small: {len(html)} chars")

        soup = BeautifulSoup(html, "html.parser")
        page_text = normalize(soup.get_text("\n", strip=True))
        if "계단식 세일" not in page_text:
            raise RuntimeError("expected step-sale marker not found")

        active, completed, diag = parse_items(soup)
        if diag["detail_id_count"] == 0:
            raise RuntimeError("no auction IDs parsed; href sample=" + repr(diag["href_sample"]))
        if not active:
            raise RuntimeError("active parser returned zero items")

        recorded = record_ended(previous, active, completed)
        meta = {
            "name": "sorishop",
            "ok": True,
            "source_url": TARGET_URL,
            "request_url": request_url,
            "final_url": r.url,
            "fetched_at_utc": now.isoformat(),
            "status_code": r.status_code,
            "title": soup.title.get_text(" ", strip=True) if soup.title else "",
            "html_chars": len(html),
            "text_chars": len(page_text),
            "sha256": hashlib.sha256(html.encode("utf-8", errors="replace")).hexdigest(),
            "recorded_ended_count": recorded,
            **diag,
        }

        HTML_PATH.write_text(html, encoding="utf-8")
        PAGE_PATH.write_text(json.dumps({"meta": meta, "page_text": page_text}, ensure_ascii=False, indent=2), encoding="utf-8")
        ITEMS_PATH.write_text(
            json.dumps(
                {"meta": meta, "count": len(active), "items": active, "completed_count": len(completed), "completed_items": completed},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        STATUS_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception as exc:
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
