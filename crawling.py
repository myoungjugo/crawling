import os, time, re
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image

# ====== 설정 ======
PAGES = [
    # 여기에 수집할 페이지들을 넣으세요
    "https://example.com/page1",
    # "https://example.com/page2",
]
OUT_DIR = Path("images_only")
SLEEP_SEC = 0.5
TIMEOUT = 25
MIN_W, MIN_H = 400, 400       # 최소 해상도(작은 썸네일 거르기)
SAVE_AS_JPG = True            # True: JPG로 통일 저장, False: 원 확장자 유지 시도
ALLOW_DOMAINS = []            # 비워두면 제한 없음. 예: ["example.com", "cdn.example.com"]

HEADERS = {
    "User-Agent": "SimpleImageDownloader/1.0 (+for research; contact: you@example.com)"
}
# ==================

def is_allowed(url: str) -> bool:
    if not ALLOW_DOMAINS:
        return True
    host = urlparse(url).netloc
    return any(host.endswith(d) for d in ALLOW_DOMAINS)

def get_soup(url: str) -> BeautifulSoup:
    time.sleep(SLEEP_SEC)
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")

def pick_from_srcset(tag, base_url: str):
    srcset = tag.get("srcset")
    if not srcset:
        return None
    best = None
    best_w = -1
    for part in srcset.split(","):
        part = part.strip()
        if " " in part:
            u, w = part.rsplit(" ", 1)
            try:
                size = int(re.sub(r"\D", "", w))
            except Exception:
                size = 0
            if size > best_w:
                best_w = size
                best = urljoin(base_url, u.strip())
        else:
            # 폭 정보 없는 항목
            if best is None:
                best = urljoin(base_url, part)
    return best

def extract_image_urls(soup: BeautifulSoup, base_url: str):
    urls = set()
    for img in soup.select("img"):
        # 1) srcset에서 큰 이미지 우선
        best = pick_from_srcset(img, base_url)
        if best:
            urls.add(best)
        # 2) src
        src = img.get("src")
        if src:
            urls.add(urljoin(base_url, src))
        # 3) lazy-load 속성 (사이트에 따라 다를 수 있음)
        for k in ("data-src", "data-original", "data-lazy", "data-owg-src"):
            v = img.get(k)
            if v:
                urls.add(urljoin(base_url, v))
    return urls

def ensure_rgb(img: Image.Image) -> Image.Image:
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")
    if img.mode == "L":
        return img.convert("RGB")
    return img

def filename_from_url(url: str) -> str:
    name = os.path.basename(urlparse(url).path) or "image"
    # 확장자 없으면 붙이기
    if "." not in name:
        name += ".jpg"
    # 깨끗한 파일명
    return re.sub(r"[^a-zA-Z0-9_\-\.]", "_", name)

def download_and_save(img_url: str, out_dir: Path):
    if not is_allowed(img_url):
        return False

    time.sleep(SLEEP_SEC)
    r = requests.get(img_url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()

    # MIME이 이미지인지 대략 점검
    ct = r.headers.get("Content-Type", "")
    if "image" not in ct:
        return False

    # Pillow로 열어서 해상도 체크 & 저장
    img = Image.open(BytesIO(r.content))
    img = ensure_rgb(img)
    w, h = img.size
    if w < MIN_W or h < MIN_H:
        return False

    if SAVE_AS_JPG:
        # JPG로 통일 저장(용량 관리/일관성)
        base = filename_from_url(img_url)
        base = re.sub(r"\.[A-Za-z0-9]+$", "", base)  # 기존 확장자 제거
        fname = f"{base}.jpg"
        path = out_dir / fname
        # 중복 방지를 위해 이미 있으면 (1), (2) 카운팅
        cnt = 1
        while path.exists():
            path = out_dir / f"{base}({cnt}).jpg"
            cnt += 1
        img.save(path, "JPEG", quality=90)
        print(f"[SAVE] {path} ({w}x{h})")
        return True
    else:
        # 원 확장자 유지 시도
        fname = filename_from_url(img_url)
        path = out_dir / fname
        cnt = 1
        while path.exists():
            name, ext = os.path.splitext(fname)
            path = out_dir / f"{name}({cnt}){ext}"
            cnt += 1
        # 포맷 추정이 애매하면 JPG로 안전 저장
        try:
            img.save(path)
        except Exception:
            path = out_dir / (os.path.splitext(path.name)[0] + ".jpg")
            img.save(path, "JPEG", quality=90)
        print(f"[SAVE] {path} ({w}x{h})")
        return True

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for page in PAGES:
        try:
            soup = get_soup(page)
            urls = extract_image_urls(soup, page)
            for u in urls:
                try:
                    if download_and_save(u, OUT_DIR):
                        total += 1
                except Exception as e:
                    print(f"[IMG ERR] {u} -> {e}")
        except Exception as e:
            print(f"[PAGE ERR] {page} -> {e}")

    if total == 0:
        print("[INFO] 저장된 이미지가 없습니다.")
    else:
        print(f"[DONE] 총 {total}개 저장됨.")

if __name__ == "__main__":
    main()
