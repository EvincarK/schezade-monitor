from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from monitor import TARGETS, TIMEOUT, cache_busted, parse_display_items

STATE_PATH = Path("snapshots/display_notify_state.json")
BOOTSTRAP_PATH = Path("snapshots/display_items.json")
SITE_URL = TARGETS["display"]


def telegram_config() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing")
    return token, chat_id


def send_telegram(text: str) -> None:
    token, chat_id = telegram_config()
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        endpoint,
        json={
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected message: {payload}")


def format_price(value: object) -> str:
    if isinstance(value, int):
        return f"{value:,}원"
    return "가격 정보 없음"


def build_message(items: list[dict]) -> str:
    lines = ["🎧 셰에라자드 신규 전시·개봉품"]
    for item in items:
        lines.extend(
            [
                "",
                str(item.get("product_name") or "상품명 없음"),
                format_price(item.get("current_price")),
                str(item.get("url") or ""),
            ]
        )
    message = "\n".join(lines).strip()
    if len(message) > 4000:
        raise RuntimeError(f"Telegram message is too long ({len(message)} chars)")
    return message


def fetch_current_items(now: datetime) -> list[dict]:
    request_url = cache_busted(SITE_URL, int(now.timestamp()))
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; schezade-monitor/1.3; +https://github.com/EvincarK/schezade-monitor)",
        "Cache-Control": "no-cache, no-store, max-age=0",
        "Pragma": "no-cache",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    }
    response = requests.get(request_url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()

    if not response.encoding or response.encoding.lower() == "iso-8859-1":
        response.encoding = response.apparent_encoding or "utf-8"

    html = response.text
    if len(html) < 1000:
        raise RuntimeError(f"display: response is suspiciously small ({len(html)} chars)")

    items = parse_display_items(BeautifulSoup(html, "html.parser"))
    if not items:
        raise RuntimeError("display: parser returned zero items")

    for item in items:
        if not isinstance(item.get("listing_id"), int):
            raise RuntimeError("display: item without integer listing_id")
        if not item.get("product_name") or not item.get("url"):
            raise RuntimeError(f"display: malformed item {item.get('listing_id')}")

    return items


def ids_from_items(items: object) -> set[int]:
    if not isinstance(items, list):
        raise RuntimeError("items is not a list")
    ids: set[int] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("listing_id"), int):
            raise RuntimeError("invalid item in snapshot")
        ids.add(item["listing_id"])
    if not ids:
        raise RuntimeError("snapshot contains zero listing ids")
    return ids


def load_previous_ids() -> tuple[set[int], str]:
    if STATE_PATH.exists():
        try:
            payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot read notifier state: {exc}") from exc
        active_ids = payload.get("active_ids")
        if not isinstance(active_ids, list) or not all(isinstance(x, int) for x in active_ids):
            raise RuntimeError("notifier state has invalid active_ids")
        if not active_ids:
            raise RuntimeError("notifier state has zero active_ids")
        return set(active_ids), "state"

    try:
        payload = json.loads(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot bootstrap from display snapshot: {exc}") from exc
    return ids_from_items(payload.get("items")), "display_items bootstrap"


def write_state(current_ids: set[int], now: datetime) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at_utc": now.isoformat(),
        "active_ids": sorted(current_ids, reverse=True),
    }
    temp_path = STATE_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(STATE_PATH)


def run_check() -> None:
    now = datetime.now(timezone.utc)
    previous_ids, source = load_previous_ids()
    items = fetch_current_items(now)
    current_ids = {item["listing_id"] for item in items}
    new_ids = current_ids - previous_ids

    if new_ids:
        new_items = [item for item in items if item["listing_id"] in new_ids]
        send_telegram(build_message(new_items))
        print(f"Sent {len(new_items)} new display item(s) to Telegram")
    else:
        print("No new display items")

    if current_ids != previous_ids or not STATE_PATH.exists():
        write_state(current_ids, now)
        print(f"Updated notifier state from {source}: {len(current_ids)} active id(s)")
    else:
        print("Notifier state unchanged")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-telegram", action="store_true")
    args = parser.parse_args()

    if args.test_telegram:
        send_telegram("✅ 셰에라자드 전시·개봉품 Telegram 알림 테스트 성공")
        print("Telegram test message sent")
        return

    run_check()


if __name__ == "__main__":
    main()
