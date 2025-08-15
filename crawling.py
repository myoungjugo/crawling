import os
import time
import threading
from queue import Queue
from urllib.parse import urlparse
from pathlib import Path

import requests
from dotenv import load_dotenv
from tqdm import tqdm

# -------------------------
# 환경변수 로드
# -------------------------
load_dotenv()

ACCESS_TOKEN = os.getenv("PIN_ACCESS_TOKEN")
BOARD_ID = os.getenv("PIN_BOARD_ID")
OUT_DIR = os.getenv("OUT_DIR", "downloads")
CONCURRENCY = max(1, int(os.getenv("CONCURRENCY", "4")))
PAGE_SIZE = max(1, min(50, int(os.getenv("PAGE_SIZE", "50"))))

if not ACCESS_TOKEN:
    raise SystemExit("환경변수 PIN_ACCESS_TOKEN 가 필요합니다 (.env 설정).")
if not BOARD_ID:
    raise SystemExit("환경변수 PIN_BOARD_ID 가 필요합니다 (.env 설정).")

BASE = "https://api.pinterest.com/v5"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

# -------------------------
# 유틸
# -------------------------
def sanitize_filename(name: str, max_len: int = 120) -> str:
    if not name:
        return "untitled"
    bad = '<>:"/\\|?*\x00-\x1F'
    for ch in bad:
        name = name.replace(ch, "_")
    name = "_".join(name.split())  # 공백 압축
    return name[:max_len].strip("_")

def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)

def backoff_sleep(retries: int):
    # 지수 백오프 (429/5xx 대응)
    time.sleep(min(60, (2 ** retries)))

# -------------------------
# Pinterest API
# -------------------------
def fetch_pins_page(board_id: str, bookmark: str | None, page_size: int = 50):
    params = {"page_size": str(page_size)}
    if bookmark:
        params["bookmark"] = bookmark

    retries = 0
    while True:
        r = requests.get(f"{BASE}/boards/{board_id}/pins", headers=HEADERS, params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and retries < 5:
            retries += 1
            backoff_sleep(retries)
            continue
        raise RuntimeError(f"핀 목록 요청 실패 {r.status_code}: {r.text}")

def pick_best_image(pin: dict) -> dict | None:
    # v5: pin.media.images.orig/xlarge/large/medium/small ...
    media = pin.get("media", {})
    images = media.get("images", {}) if isinstance(media, dict) else {}

    order = ["orig", "xlarge", "large", "medium", "small"]
    for key in order:
        img = images.get(key)
        if isinstance(img, dict) and img.get("url"):
            return img

    # 예외 구조 대비: pin.images.* 가 있을 수 있음
    fallback = pin.get("images", {})
    if isinstance(fallback, dict) and fallback:
        sizes = sorted(
            [v for v in fallback.values() if isinstance(v, dict) and v.get("url")],
            key=lambda x: (x.get("width") or 0),
            reverse=True,
        )
        if sizes:
            return sizes[0]
    return None

def stream_download(url: str, filepath: Path):
    retries = 0
    while True:
        with requests.get(url, stream=True, timeout=60) as r:
            if r.status_code == 200:
                with open(filepath, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                return
            if r.status_code in (429, 500, 502, 503, 504) and retries < 5:
                retries += 1
                backoff_sleep(retries)
                continue
            raise RuntimeError(f"다운로드 실패 {r.status_code} {url}")

# -------------------------
# 작업 큐(멀티스레드)
# -------------------------
class Downloader:
    def __init__(self, out_dir: Path, concurrency: int = 4):
        self.out_dir = out_dir
        self.q: Queue[dict] = Queue()
        self.bar = None
        self.total = 0
        self.lock = threading.Lock()
        self.threads = []
        self.concurrency = concurrency

    def worker(self):
        while True:
            job = self.q.get()
            if job is None:
                self.q.task_done()
                break
            pin = job["pin"]
            try:
                img = pick_best_image(pin)
                if not img or not img.get("url"):
                    # 이미지 없는 핀은 스킵
                    self._tick()
                    self.q.task_done()
                    continue

                url = img["url"]
                ext = Path(urlparse(url).path).suffix or ".jpg"

                title = pin.get("title") or pin.get("description") or ""
                base = sanitize_filename(f'{pin.get("id","")}_{title}')
                if not base:
                    base = str(pin.get("id", "pin"))
                filepath = self.out_dir / f"{base}{ext}"

                # 파일명 중복 방지
                idx = 1
                while filepath.exists():
                    filepath = self.out_dir / f"{base}_{idx}{ext}"
                    idx += 1

                stream_download(url, filepath)
            except Exception as e:
                # 실패해도 다음 작업 진행
                # 필요시 로그 파일에 기록하도록 수정 가능
                pass
            finally:
                self._tick()
                self.q.task_done()

    def _tick(self):
        if self.bar:
            with self.lock:
                self.bar.update(1)

    def run(self, pins: list[dict]):
        self.total = len(pins)
        ensure_dir(self.out_dir)
        self.bar = tqdm(total=self.total, unit="img", desc="다운로드")

        # 워커 시작
        for _ in range(self.concurrency):
            t = threading.Thread(target=self.worker, daemon=True)
            t.start()
            self.threads.append(t)

        # 큐 적재
        for pin in pins:
            self.q.put({"pin": pin})

        # 종료 신호
        for _ in range(self.concurrency):
            self.q.put(None)

        self.q.join()
        for t in self.threads:
            t.join()
        self.bar.close()

# -------------------------
# 실행
# -------------------------
def main():
    out_dir = Path(OUT_DIR)
    ensure_dir(out_dir)

    pins = []
    bookmark = None
    total_pages = 0

    while True:
        data = fetch_pins_page(BOARD_ID, bookmark, PAGE_SIZE)
        items = data.get("items", [])
        pins.extend(items)
        bookmark = data.get("bookmark")
        total_pages += 1
        if not bookmark or not items:
            break

    if not pins:
        print("가져올 핀이 없습니다. (보드 ID/권한/토큰/스코프 확인)")
        return

    print(f"총 {len(pins)}개 핀 수집, {total_pages} 페이지.")
    dl = Downloader(out_dir=out_dir, concurrency=CONCURRENCY)
    dl.run(pins)
    print("✅ 완료!")

if __name__ == "__main__":
    main()