"""
Goodreads ISBN-13 Web Scraping Pipeline (Automated CSV Mode with Logging)
========================================================================
CREP Dataset Collection Script — reads ISBNs automatically from CSV,
strictly uses Goodreads, logs all actions, stores genres in metadata JSON,
appends all metadata records into a single consolidated JSON file,
and saves outputs adhering to mandatory naming conventions.
"""

import os
import re
import sys
import json
import time
import random
import argparse

import requests
from bs4 import BeautifulSoup

try:
    import isbnlib  # optional; used only for origin_country inference
except ImportError:
    isbnlib = None

# --------------------------------------------------------------------------- #
# CONFIG & LOGGING SETUP
# --------------------------------------------------------------------------- #

BASE_DIR = "data"
FOLDERS = {
    "metadata": os.path.join(BASE_DIR, "book_metadata"),
    "coverpage": os.path.join(BASE_DIR, "book_coverpage"),
    "blurb": os.path.join(BASE_DIR, "book_blurb"),
    "reviews": os.path.join(BASE_DIR, "book_reviews"),
    "genres": os.path.join(BASE_DIR, "genres"),
}

LOG_FILE = "scraper_execution.log"
GOODREADS_METADATA_FILE = os.path.join(FOLDERS["metadata"], "goodreads_metadata.json")

def log(message: str):
    """Prints message to console and appends it to the execution log file."""
    print(message)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except Exception:
        pass

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

MIN_REVIEWS = 25
USE_SELENIUM_FOR_REVIEWS = True   # set False to only try the fast requests-based pass
REQUEST_DELAY_RANGE = (1.5, 3.5)  # polite delay between requests, in seconds

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

_DRIVER = None


def _make_chrome_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--log-level=3")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    options.page_load_strategy = 'eager'

    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(60)
        return driver
    except Exception as e:
        log(f"[reviews] Selenium's built-in driver manager failed ({e}); trying webdriver-manager...")

    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.set_page_load_timeout(60)
    return driver


def get_driver():
    """Lazily create (once) and return the shared Selenium driver, recreating if crashed."""
    global _DRIVER
    if _DRIVER is None:
        _DRIVER = _make_chrome_driver()
    else:
        try:
            _DRIVER.current_url
        except Exception:
            log("[warn] Existing Chrome driver session crashed or closed. Recreating...")
            close_driver()
            _DRIVER = _make_chrome_driver()
    return _DRIVER


def close_driver():
    global _DRIVER
    if _DRIVER is not None:
        try:
            _DRIVER.quit()
        except Exception:
            pass
        _DRIVER = None


def _looks_blocked(html: str) -> bool:
    """Heuristic check for bot-block / captcha / consent interstitial pages."""
    if not html:
        return True
    lowered = html.lower()
    markers = ["captcha", "unusual traffic", "are you a robot", "access denied", "just a moment"]
    return any(m in lowered for m in markers) or len(html) < 500


# --------------------------------------------------------------------------- #
# SETUP
# --------------------------------------------------------------------------- #

def setup_folders():
    for path in FOLDERS.values():
        os.makedirs(path, exist_ok=True)


def load_existing_metadata_records(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def append_metadata_record(metadata: dict, path: str) -> None:
    records = load_existing_metadata_records(path)
    records = [record for record in records if record.get("isbn13") != metadata.get("isbn13")]
    records.append(metadata)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def polite_sleep():
    time.sleep(random.uniform(*REQUEST_DELAY_RANGE))


# --------------------------------------------------------------------------- #
# STEP 0: ISBN-13 -> Goodreads book URL (Strictly Goodreads Only)
# --------------------------------------------------------------------------- #

def get_book_url_from_isbn(isbn13: str) -> str | None:
    try:
        r = SESSION.get(
            "https://www.goodreads.com/book/auto_complete",
            params={"format": "json", "q": isbn13},
            timeout=15,
        )
        if r.ok:
            results = r.json()
            if results:
                first = results[0]
                book_id = first.get("bookId") or first.get("id")
                title_slug = re.sub(r"[^a-zA-Z0-9]+", "-", (first.get("title") or "book")).strip("-")
                if book_id:
                    return f"https://www.goodreads.com/book/show/{book_id}-{title_slug}"
    except (requests.RequestException, ValueError):
        pass

    polite_sleep()

    try:
        r = SESSION.get(f"https://www.goodreads.com/book/isbn/{isbn13}", timeout=15, allow_redirects=True)
        if r.ok and "/book/show/" in r.url:
            return r.url
    except requests.RequestException:
        pass

    polite_sleep()

    try:
        search_url = f"https://www.goodreads.com/search?q={isbn13}"
        r = SESSION.get(search_url, timeout=15)
        r.raise_for_status()

        if _looks_blocked(r.text):
            log(f"[warn] search page for {isbn13} looks blocked/captcha'd (status={r.status_code}, len={len(r.text)})")
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        link = soup.select_one("a.bookTitle")
        if link and link.get("href"):
            return "https://www.goodreads.com" + link["href"]

        if "/book/show/" in r.url:
            return r.url
    except requests.RequestException as e:
        log(f"[warn] search request failed for {isbn13}: {e}")

    return None


def fetch_soup(url: str) -> BeautifulSoup:
    r = SESSION.get(url, timeout=15)
    r.raise_for_status()
    if _looks_blocked(r.text):
        log(f"[warn] book page looks blocked/captcha'd (status={r.status_code}, len={len(r.text)})")
    return BeautifulSoup(r.text, "html.parser")


def _page_incomplete(soup: BeautifulSoup, ld: dict) -> bool:
    has_title = bool(_text(soup.select_one("h1[data-testid='bookTitle']")))
    return not ld and not has_title


def fetch_html_via_selenium(url: str, wait_css: str = "h1[data-testid='bookTitle']", timeout: int = 15) -> str | None:
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, WebDriverException
    except ImportError:
        log("[warn] selenium not installed; cannot use browser fallback.")
        return None

    try:
        driver = get_driver()
    except Exception as e:
        log(f"[warn] could not start Chrome for browser fallback: {e}")
        return None

    try:
        driver.get(url)
        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
            )
        except TimeoutException:
            log(f"[warn] page did not fully load within {timeout}s via browser fallback.")
        return driver.page_source
    except WebDriverException as e:
        log(f"[warn] browser fallback failed to load page: {e}")
        return None


def extract_ld_json(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("@type") in ("Book", "Product"):
            return data
    return {}


def extract_next_data(soup: BeautifulSoup) -> dict:
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return {}
    try:
        return json.loads(tag.string)
    except json.JSONDecodeError:
        return {}


def _deep_find(obj, target_keys: list[str], max_nodes: int = 20000):
    target_keys_lower = {k.lower() for k in target_keys}
    stack = [obj]
    visited = 0
    while stack and visited < max_nodes:
        current = stack.pop()
        visited += 1
        if isinstance(current, dict):
            for k, v in current.items():
                if isinstance(k, str) and k.lower() in target_keys_lower:
                    if isinstance(v, str) and v.strip():
                        return v.strip()
                    if isinstance(v, dict):
                        nested_name = v.get("name") or v.get("value")
                        if isinstance(nested_name, str) and nested_name.strip():
                            return nested_name.strip()
            for v in current.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(current, list):
            for item in current:
                if isinstance(item, (dict, list)):
                    stack.append(item)
    return None


# --------------------------------------------------------------------------- #
# ORIGIN COUNTRY
# --------------------------------------------------------------------------- #

_ISBN_GROUP_FALLBACK = {
    "0": "English-language region (US/UK/etc.)",
    "1": "English-language region (US/UK/etc.)",
    "2": "French-language region",
    "3": "German-language region",
    "4": "Japan",
    "5": "Russian-language region",
    "7": "China",
    "80": "Czech Republic / Slovakia",
    "81": "India",
    "82": "Norway",
    "83": "Poland",
    "84": "Spain",
    "85": "Brazil",
    "86": "Former Yugoslav region",
    "87": "Denmark",
    "88": "Italy",
    "89": "South Korea",
    "90": "Netherlands / Belgium (Dutch)",
    "91": "Sweden",
    "93": "India",
    "94": "Netherlands",
}


def infer_country_from_isbn(isbn13: str) -> str:
    isbn13 = isbn13.strip().replace("-", "").replace(" ", "")

    if isbnlib is not None:
        try:
            info = isbnlib.info(isbn13)
            if info:
                return info
        except Exception:
            pass

    digits = isbn13[3:] if isbn13[:3] in ("978", "979") else isbn13
    for prefix_len in (2, 1):
        prefix = digits[:prefix_len]
        if prefix in _ISBN_GROUP_FALLBACK:
            return _ISBN_GROUP_FALLBACK[prefix]

    return ""


def extract_genres_list(soup: BeautifulSoup) -> list[str]:
    genres = []
    selectors = [
        "span.BookPageMetadataSection__genreButton a span",
        "a.BookPageMetadataSection__genreButton",
        "div.BookPageMetadataSection__genres a.Button--tag span.Button__labelItem",
    ]

    for sel in selectors:
        for tag in soup.select(sel):
            g = _text(tag)
            if g and g not in genres:
                genres.append(g)
        if genres:
            break

    return genres


# --------------------------------------------------------------------------- #
# TASK 1: BOOK METADATA (APPENDS TO consolidated goodreads_metadata.json)
# --------------------------------------------------------------------------- #

def scrape_metadata(soup: BeautifulSoup, ld: dict, isbn13: str, book_url: str) -> dict:
    title = ld.get("name") or _text(soup.select_one("h1[data-testid='bookTitle']"))

    authors = []
    for a in soup.select("span.ContributorLink__name"):
        name = _text(a)
        if name and name not in authors:
            authors.append(name)
    if not authors and isinstance(ld.get("author"), list):
        authors = [a.get("name") for a in ld["author"] if a.get("name")]
    elif not authors and isinstance(ld.get("author"), dict):
        authors = [ld["author"].get("name")]

    language = ld.get("inLanguage")
    publisher, pub_date = _parse_publication_info(soup)

    if not publisher:
        ld_publisher = ld.get("publisher")
        if isinstance(ld_publisher, str) and ld_publisher.strip():
            publisher = ld_publisher.strip()
        elif isinstance(ld_publisher, dict):
            name = ld_publisher.get("name")
            if isinstance(name, str) and name.strip():
                publisher = name.strip()

    next_data = extract_next_data(soup)
    if not publisher:
        publisher = _deep_find(next_data, ["publisher", "publisherName"])

    origin_country = _deep_find(next_data, ["country", "publicationCountry", "countryOfOrigin"]) or ""
    if not origin_country:
        origin_country = infer_country_from_isbn(isbn13)

    genres = extract_genres_list(soup)

    metadata = {
        "isbn13": isbn13,
        "title": title,
        "authors": authors,
        "publisher": publisher,
        "origin_country": origin_country,
        "date_of_publication": pub_date,
        "language": language,
        "genres": genres,
        "source_url": book_url,
        "sources": ["Goodreads"],
    }

    # Append record into the single consolidated JSON file
    append_metadata_record(metadata, GOODREADS_METADATA_FILE)

    log(f"[metadata] appended record to -> {GOODREADS_METADATA_FILE}")
    return metadata


def _text(tag):
    return tag.get_text(strip=True) if tag else None


def _parse_publication_info(soup: BeautifulSoup):
    tag = soup.select_one(".FeaturedDetails p[data-testid='publicationInfo']") \
        or soup.select_one("p[data-testid='publicationInfo']")
    text = _text(tag)
    if not text:
        return None, None

    match = re.match(
        r"^(?:First published|Published)\s+(?P<date>.*?)(?:\s+by\s+(?P<publisher>.+))?$",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, text

    date_str = (match.group("date") or "").strip() or None
    publisher = (match.group("publisher") or "").strip() or None
    return publisher, date_str


# --------------------------------------------------------------------------- #
# TASK 2: BOOK COVERPAGE (Strict Naming: <isbn13>_cp_<source>_1.jpg)
# --------------------------------------------------------------------------- #

def scrape_coverpage(soup: BeautifulSoup, ld: dict, isbn13: str, source: str = "goodreads"):
    image_url = ld.get("image")
    if not image_url:
        img_tag = soup.select_one("img.ResponsiveImage, img.BookCover__image, img[data-testid='bookCoverImage']")
        image_url = img_tag["src"] if img_tag else None

    if not image_url:
        og_img = soup.select_one("meta[property='og:image']")
        if og_img and og_img.get("content"):
            image_url = og_img["content"]

    if not image_url:
        log(f"[coverpage] no image found for {isbn13}")
        return

    try:
        resp = SESSION.get(image_url, timeout=15)
        resp.raise_for_status()

        out_path = os.path.join(FOLDERS["coverpage"], f"{isbn13}_cp_{source}_1.jpg")
        with open(out_path, "wb") as f:
            f.write(resp.content)

        log(f"[coverpage] saved -> {out_path}")
    except Exception as e:
        log(f"[coverpage] failed to download image for {isbn13}: {e}")


# --------------------------------------------------------------------------- #
# TASK 3: BOOK BLURB (Strict Naming: <isbn13>_b_<source>_1.txt)
# --------------------------------------------------------------------------- #

def scrape_blurb(soup: BeautifulSoup, ld: dict, isbn13: str, source: str = "goodreads"):
    blurb = ld.get("description")
    if not blurb:
        desc_tag = soup.select_one("div[data-testid='description'] span")
        blurb = _text(desc_tag)

    if not blurb:
        log(f"[blurb] no description found for {isbn13}")
        return

    blurb = BeautifulSoup(blurb, "html.parser").get_text("\n").strip()

    out_path = os.path.join(FOLDERS["blurb"], f"{isbn13}_b_{source}_1.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(blurb)

    log(f"[blurb] saved -> {out_path}")


# --------------------------------------------------------------------------- #
# TASK 4: BOOK REVIEWS (Strict Naming: <isbn13>_r_<source>_<n>.txt)
# --------------------------------------------------------------------------- #

def scrape_reviews_requests(soup: BeautifulSoup) -> list[str]:
    reviews = []
    for card in soup.select("article.ReviewCard"):
        body = card.select_one("section.ReviewText, span.Formatted")
        text = _text(body)
        if text:
            reviews.append(text)
    return reviews


def scrape_reviews_selenium(book_url: str, min_reviews: int = MIN_REVIEWS) -> list[str]:
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, WebDriverException
    except ImportError:
        log("[reviews] selenium not installed.")
        return []

    try:
        driver = get_driver()
    except WebDriverException as e:
        log(f"[reviews] could not start Chrome: {e}")
        return []

    reviews: list[str] = []

    try:
        if driver.current_url != book_url:
            driver.get(book_url)
            time.sleep(3)

        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "article.ReviewCard"))
            )
        except TimeoutException:
            pass

        scroll_attempts = 0
        while len(reviews) < min_reviews and scroll_attempts < 12:
            cards = driver.find_elements(By.CSS_SELECTOR, "article.ReviewCard")
            reviews = []
            for card in cards:
                try:
                    text = card.find_element(
                        By.CSS_SELECTOR, "section.ReviewText, span.Formatted"
                    ).text.strip()
                    if text:
                        reviews.append(text)
                except Exception:
                    continue

            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2.5)
            scroll_attempts += 1

    except WebDriverException as e:
        log(f"[reviews] Selenium session error: {e}")

    return reviews[:max(min_reviews, len(reviews))]


def scrape_reviews(soup: BeautifulSoup, book_url: str, isbn13: str, source: str = "goodreads"):
    reviews = scrape_reviews_requests(soup)

    if len(reviews) < MIN_REVIEWS and USE_SELENIUM_FOR_REVIEWS:
        log(f"[reviews] only {len(reviews)} found via requests; trying Selenium fallback...")
        selenium_reviews = scrape_reviews_selenium(book_url, MIN_REVIEWS)
        if len(selenium_reviews) > len(reviews):
            reviews = selenium_reviews

    if not reviews:
        log(f"[reviews] no reviews found for {isbn13}")
        return

    for i, review_text in enumerate(reviews[:max(MIN_REVIEWS, len(reviews))], start=1):
        out_path = os.path.join(FOLDERS["reviews"], f"{isbn13}_r_{source}_{i}.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(review_text)

    log(f"[reviews] saved {min(len(reviews), MIN_REVIEWS)} reviews -> {FOLDERS['reviews']}/")


# --------------------------------------------------------------------------- #
# PIPELINE
# --------------------------------------------------------------------------- #

def run_for_isbn(isbn13: str):
    isbn13 = isbn13.strip().replace("-", "")
    log(f"\n=== Processing ISBN-13: {isbn13} ===")

    book_url = get_book_url_from_isbn(isbn13)
    if not book_url:
        log(f"[!] Could not find a Goodreads page for ISBN {isbn13}. Skipping.")
        return

    polite_sleep()
    soup = fetch_soup(book_url)
    ld = extract_ld_json(soup)

    if _page_incomplete(soup, ld):
        log(f"[warn] requests-based fetch for {isbn13} looks incomplete... retrying with browser fallback...")
        try:
            html = fetch_html_via_selenium(book_url)
            if html:
                soup = BeautifulSoup(html, "html.parser")
                ld = extract_ld_json(soup)
        except Exception as e:
            log(f"[warn] Browser fallback encountered an error for {isbn13}: {e}")

    try:
        scrape_metadata(soup, ld, isbn13, book_url)
        scrape_coverpage(soup, ld, isbn13, source="goodreads")
        scrape_blurb(soup, ld, isbn13, source="goodreads")
        scrape_reviews(soup, book_url, isbn13, source="goodreads")
    except Exception as e:
        log(f"[!] Error during data extraction components for {isbn13}: {e}")


CSV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "2602193001.csv")


def load_isbns_from_csv(csv_path: str) -> list[str]:
    import csv

    isbns = []
    if not os.path.exists(csv_path):
        log(f"[error] CSV file not found at: {csv_path}")
        return isbns

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            candidate = row[0].strip().strip('"').strip("'")
            cleaned = candidate.replace("-", "").replace(" ", "")
            if cleaned.isdigit() and len(cleaned) in (10, 13):
                isbns.append(cleaned)
            else:
                log(f"[skip] not a valid ISBN, skipping: {candidate!r}")
    return isbns


def main():
    parser = argparse.ArgumentParser(description="Automated Goodreads ISBN-13 Scraper (CSV Batch Mode)")
    parser.add_argument("--csv-file", default=CSV_FILE, help=f"Path to the CSV file (default: {CSV_FILE})")
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N ISBNs")
    parser.add_argument("--start-from", type=int, default=0, help="Skip the first N ISBNs in the CSV")
    args = parser.parse_args()

    setup_folders()

    log(f"[info] Automatically loading ISBNs from CSV: {args.csv_file}")
    log(f"[info] All execution logs are being saved to: {LOG_FILE}")
    isbn_list = load_isbns_from_csv(args.csv_file)
    log(f"[info] Found {len(isbn_list)} valid ISBNs in CSV.")

    if not isbn_list:
        log("[error] No valid ISBNs found or CSV file is empty/missing. Exiting.")
        sys.exit(1)

    if args.start_from:
        isbn_list = isbn_list[args.start_from:]
    if args.limit is not None:
        isbn_list = isbn_list[:args.limit]

    total = len(isbn_list)
    log(f"[info] {total} ISBN(s) queued for automated processing.\n")

    try:
        for idx, isbn in enumerate(isbn_list, start=1):
            log(f"\n{'='*60}")
            log(f"[progress] {idx}/{total} — ISBN: {isbn}")
            log(f"{'='*60}")
            try:
                run_for_isbn(isbn)
            except Exception as e:
                log(f"[!] Error processing {isbn}: {e}")
            polite_sleep()
    finally:
        close_driver()

    log(f"\n[done] Finished processing {total} ISBNs from CSV dataset.")


if __name__ == "__main__":
    main()