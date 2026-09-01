# schezade-monitor

셰에라자드의 두 페이지를 GitHub Actions가 직접 HTTP GET으로 주기적으로 가져와 최신 스냅샷을 저장합니다.

## 감시 대상

- 전시·개봉품: `https://www.schezade.co.kr/pagegen/v2/custom/display/index.php`
- 계단식 세일: `https://www.schezade.co.kr/pagegen/v2/custom/step-sale/index.php`

## 동작

GitHub Actions가 매시간 7분에 실행됩니다. 각 요청에는 cache-busting query와 no-cache 헤더를 사용합니다.

성공한 대상은 아래 파일을 갱신합니다.

- `snapshots/display.html` — 전시·개봉품 원본 HTML
- `snapshots/display.json` — fetch 시각, 응답 메타데이터, 정리된 본문, 링크 목록
- `snapshots/step_sale.html` — 계단식 세일 원본 HTML
- `snapshots/step_sale.json` — fetch 시각, 응답 메타데이터, 정리된 본문, 링크 목록
- `snapshots/status.json` — 두 대상의 이번 fetch 성공/실패 여부와 시각

두 대상 중 하나가 실패해도 다른 대상의 정상 스냅샷은 갱신됩니다. 실패한 대상의 기존 HTML/JSON은 덮어쓰지 않고 유지하며, `status.json`에서 해당 대상의 `ok: false`를 기록합니다.

## ChatGPT 작업에서 사용할 때

ChatGPT 예약 작업은 셰에라자드 원본 페이지를 직접 웹 검색/open하지 말고 GitHub 연결을 통해 이 저장소의 `snapshots/status.json`과 해당 JSON/HTML 파일을 읽는 것을 1차 데이터 소스로 사용합니다.

각 예약 작업은 자기 대상의 `status.json -> targets.<name>.ok`가 true이고 `fetched_at_utc`가 충분히 최근인 경우에만 상태 판정에 사용합니다. 오래된 스냅샷이나 fetch 실패 상태면 조회 실패로 처리하고 기존 baseline/state를 변경하지 않습니다.
