import flask
from flask import Flask, render_template, request, send_file, abort, url_for
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor
import os
import time
import uuid
from werkzeug.utils import secure_filename
import validators
from functools import lru_cache
import logging

# --- Configuration ---
app = Flask(__name__)
# مسیر instance برای فایل‌های موقت (مانند آپلود و نتایج)
INSTANCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
app.config['INSTANCE_FOLDER'] = INSTANCE_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
MAX_URLS = 20  # حداکثر تعداد URL برای بررسی از هر سایت
TIMEOUT = 10  # افزایش تایم‌اوت کلی
TIMEOUT_PER_URL = 5  # تایم‌اوت برای هر URL جداگانه
MAX_WORKERS = min(os.cpu_count() * 2 + 1, 10)  # تنظیم تعداد ورکرها بر اساس CPU (حداکثر 10)

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Check if lxml is installed ---
try:
    import lxml
    DEFAULT_PARSER = 'lxml'
except ImportError:
    logger.warning("lxml not found, using html.parser. Install lxml for potentially faster parsing.")
    DEFAULT_PARSER = 'html.parser'

# --- Utility Functions ---
def cleanup_old_files(folder, max_age_hours=24):
    """Clean up files older than max_age_hours in the specified folder."""
    now = time.time()
    for filename in os.listdir(folder):
        file_path = os.path.join(folder, filename)
        if os.path.isfile(file_path):
            file_age = now - os.path.getmtime(file_path)
            if file_age > max_age_hours * 3600:  # تبدیل ساعت به ثانیه
                try:
                    os.remove(file_path)
                    logger.info(f"Removed old file: {file_path}")
                except OSError as e:
                    logger.error(f"Error removing old file {file_path}: {e}")

@lru_cache(maxsize=100)
def get_urls(website):
    """Get URLs from website with caching and improved error logging."""
    logger.info(f"Attempting to fetch URLs from: {website}")
    if not validators.url(website):
        logger.error(f"Invalid URL provided: {website}")
        return []

    urls = set()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; KeywordCheckerBot/1.0)'}
        res = requests.get(website, timeout=TIMEOUT, headers=headers, allow_redirects=True)
        res.raise_for_status()

        res.encoding = res.apparent_encoding
        soup = BeautifulSoup(res.text, DEFAULT_PARSER)

        urls.add(website.strip('/'))  # اضافه کردن URL پایه

        for a in soup.select('a[href]'):
            try:
                href = a['href']
                full_url = urljoin(website, href).split('#')[0]

                if full_url.startswith(website.strip('/')) and validators.url(full_url):
                    urls.add(full_url)
                if len(urls) >= MAX_URLS * 2:
                    break
            except Exception as link_err:
                logger.warning(f"Could not process link {a.get('href', 'N/A')} on {website}: {link_err}")
                continue

    except requests.Timeout:
        logger.error(f"Timeout error fetching URLs from {website} after {TIMEOUT} seconds.")
        urls.add(website.strip('/'))
    except requests.RequestException as e:
        logger.error(f"Error fetching URLs from {website}: {e}")
        urls.add(website.strip('/'))
    except Exception as e:
        logger.error(f"Unexpected error during URL fetching from {website}: {e}")
        urls.add(website.strip('/'))

    final_urls = list(urls)
    logger.info(f"Found {len(final_urls)} unique URLs initially for {website}. Limiting to {MAX_URLS}.")
    return final_urls[:MAX_URLS]

def check_keyword(keyword, urls_to_check, website_base_url):
    """Check keyword presence with optimized scoring and better logging."""
    if not urls_to_check:
        logger.warning(f"No URLs available to check for keyword '{keyword}' on {website_base_url}")
        return {"keyword": keyword, "found": False, "url": "-", "score": 0, "status": "No URLs available"}

    keyword_original = keyword
    keyword = str(keyword).lower().strip()

    if not keyword:
        logger.warning("Empty keyword received.")
        return {"keyword": keyword_original, "found": False, "url": "-", "score": 0, "status": "Empty Keyword"}

    variations = {
        'exact': keyword,
        'dash': keyword.replace(" ", "-"),
        'nospace': keyword.replace(" ", "")
    }
    best_score = 0
    best_url = "-"
    status = "Not Found"

    logger.debug(f"Checking keyword '{keyword}' on {len(urls_to_check)} URLs for {website_base_url}")

    for url in urls_to_check:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (compatible; KeywordCheckerBot/1.0)'}
            res = requests.get(url, timeout=TIMEOUT_PER_URL, headers=headers, allow_redirects=True)

            if res.status_code != 200:
                logger.warning(f"Got status code {res.status_code} for URL: {url} while checking keyword '{keyword}'")
                continue

            res.encoding = res.apparent_encoding
            content = res.text.lower()
            score = 0

            url_lower = url.lower()
            if variations['exact'] in url_lower:
                score += 2
                logger.debug(f"Keyword '{keyword}' variation 'exact' found in URL: {url}")
            elif any(var in url_lower for var in (variations['dash'], variations['nospace'])):
                score += 1
                logger.debug(f"Keyword '{keyword}' variation 'dash/nospace' found in URL: {url}")

            soup = BeautifulSoup(content, DEFAULT_PARSER)
            title_tag = soup.find('title')
            title = title_tag.string.lower() if title_tag and title_tag.string else ''
            h1_tag = soup.find('h1')
            h1 = h1_tag.string.lower() if h1_tag and h1_tag.string else ''

            if variations['exact'] in title:
                score += 3
                logger.debug(f"Keyword '{keyword}' variation 'exact' found in Title: {title} on URL: {url}")
            if variations['exact'] in h1:
                score += 4
                logger.debug(f"Keyword '{keyword}' variation 'exact' found in H1: {h1} on URL: {url}")
            if variations['exact'] in content:
                score += 5
                logger.debug(f"Keyword '{keyword}' variation 'exact' found in content body of URL: {url}")

            if score > best_score:
                best_score = score
                best_url = url
                status = "Found"
                logger.info(f"New best score {best_score} for keyword '{keyword}' found at URL: {url}")

        except requests.Timeout:
            logger.warning(f"Timeout error checking URL: {url} for keyword '{keyword}' after {TIMEOUT_PER_URL} seconds.")
            if best_score == 0:
                status = "Timeout on check"
            continue
        except requests.RequestException as e:
            logger.warning(f"Request error checking URL: {url} for keyword '{keyword}': {e}")
            if best_score == 0:
                status = "Request Error on check"
            continue
        except Exception as e:
            logger.error(f"Unexpected error checking URL {url} for keyword '{keyword}': {e}")
            if best_score == 0:
                status = "Processing Error"
            continue

    if best_score == 0 and status == "Not Found":
        logger.info(f"Keyword '{keyword}' was not found in any of the checked URLs for {website_base_url}")

    return {"keyword": keyword_original, "found": best_score > 0, "url": best_url, "score": best_score, "status": status}

# --- Flask Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    results = []
    download_filename = None
    error = None
    website_url = None  # اصلاح خطای سینتکسی

    if request.method == 'POST':
        website_url = request.form.get('website', '').strip()
        if not validators.url(website_url):
            error = "آدرس وب‌سایت نامعتبر است. لطفاً با https:// یا http:// وارد کنید."
            return render_template("index.html", error=error, max_urls=MAX_URLS)

        if 'file' not in request.files or not request.files['file'].filename:
            error = "فایلی آپلود نشده است."
            return render_template("index.html", error=error, website=website_url, max_urls=MAX_URLS)

        file = request.files['file']
        if not file.filename.endswith('.xlsx'):
            error = "لطفاً یک فایل اکسل با پسوند .xlsx آپلود کنید."
            return render_template("index.html", error=error, website=website_url, max_urls=MAX_URLS)

        original_filename = secure_filename(file.filename)
        temp_id = uuid.uuid4()
        temp_filename = f"{temp_id}_{original_filename}"
        temp_path = os.path.join(app.config['INSTANCE_FOLDER'], temp_filename)

        os.makedirs(app.config['INSTANCE_FOLDER'], exist_ok=True)

        try:
            file.save(temp_path)
            logger.info(f"File '{original_filename}' uploaded successfully and saved as '{temp_filename}'.")

            df = pd.read_excel(temp_path)
            if df.empty or df.shape[1] == 0:
                error = "فایل اکسل خالی است یا ستونی ندارد."
                raise ValueError(error)

            keywords = df.iloc[:, 0].dropna().astype(str).tolist()
            if not keywords:
                error = "ستون اول فایل اکسل کلمه کلیدی معتبری ندارد."
                raise ValueError(error)

            logger.info(f"Extracted {len(keywords)} keywords to check.")

            urls_to_check = get_urls(website_url)
            if not urls_to_check:
                error = f"نمی‌توان URL ها را از {website_url} استخراج کرد. لطفاً آدرس را بررسی کنید."
                raise ValueError(error)

            logger.info(f"Starting keyword check process on {len(urls_to_check)} URLs using {MAX_WORKERS} workers.")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                results = list(executor.map(lambda k: check_keyword(k, urls_to_check, website_url), keywords))

            logger.info("Keyword checking process completed.")

            timestamp = time.strftime("%Y%m%d-%H%M%S")
            output_filename = f"results_{secure_filename(website_url.replace('https://','').replace('http://','').split('/')[0])}_{timestamp}.xlsx"
            output_path = os.path.join(app.config['INSTANCE_FOLDER'], output_filename)

            pd.DataFrame(results).to_excel(output_path, index=False)
            logger.info(f"Results saved to '{output_filename}'.")
            download_filename = output_filename

            # پاکسازی فایل‌های قدیمی بعد از ذخیره نتایج
            cleanup_old_files(app.config['INSTANCE_FOLDER'], max_age_hours=24)

        except FileNotFoundError:
            logger.error(f"Error reading uploaded file at {temp_path}. It might have been removed prematurely.")
            error = "خطا در خواندن فایل آپلود شده."
        except ValueError as ve:
            logger.error(f"Value error during processing: {ve}")
            error = str(ve)
        except Exception as e:
            logger.exception(f"An unexpected error occurred during processing for website '{website_url}'")
            error = "یک خطای پیش‌بینی نشده در حین پردازش رخ داد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                    logger.info(f"Temporary uploaded file '{temp_filename}' removed.")
                except OSError as oe:
                    logger.error(f"Error removing temporary file {temp_path}: {oe}")

    return render_template("index.html",
                          results=results,
                          download_filename=download_filename,
                          error=error,
                          website=website_url,
                          max_urls=MAX_URLS)

@app.route('/download/<filename>')
def download(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        logger.warning(f"Attempt to download potentially unsafe filename: {filename}")
        abort(404)

    output_path = os.path.join(app.config['INSTANCE_FOLDER'], safe_filename)
    logger.info(f"Download request for file: {safe_filename}")

    if not os.path.exists(output_path):
        logger.error(f"Download failed. File not found: {output_path}")
        abort(404)

    try:
        return send_file(output_path, as_attachment=True, download_name=safe_filename)
    except Exception as e:
        logger.exception(f"Error sending file {safe_filename} for download.")
        abort(500)

# --- Main Execution ---
if __name__ == '__main__':
    os.makedirs(app.config['INSTANCE_FOLDER'], exist_ok=True)
    logger.info(f"Instance folder set to: {app.config['INSTANCE_FOLDER']}")
    # پاکسازی فایل‌های قدیمی در زمان استارت اپلیکیشن
    cleanup_old_files(app.config['INSTANCE_FOLDER'], max_age_hours=24)
    app.run(host='0.0.0.0', port=5000, debug=False)
