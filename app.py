import flask
from flask import Flask, render_template, request, send_file, abort, url_for
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor
import os
from werkzeug.utils import secure_filename
import validators
from functools import lru_cache
import logging
import uuid # برای نام فایل منحصر به فرد
import time # برای اضافه کردن به نام فایل

# --- Configuration ---
app = Flask(__name__)
# مسیر instance برای فایل‌های موقت (مانند آپلود و نتایج)
INSTANCE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
app.config['INSTANCE_FOLDER'] = INSTANCE_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
MAX_URLS = 20 # حداکثر تعداد URL برای بررسی از هر سایت
TIMEOUT = 10 # افزایش تایم‌اوت کلی
TIMEOUT_PER_URL = 5 # تایم‌اوت برای هر URL جداگانه
MAX_WORKERS = 10 # حداکثر تعداد تردها برای بررسی کلمات کلیدی

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

# --- Functions ---
@lru_cache(maxsize=100)
def get_urls(website):
    """Get URLs from website with caching and improved error logging"""
    logger.info(f"Attempting to fetch URLs from: {website}")
    if not validators.url(website):
        logger.error(f"Invalid URL provided: {website}")
        # برگرداندن لیست خالی منطقی‌تر است وقتی URL نامعتبر است
        return []

    urls = set()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; KeywordCheckerBot/1.0)'}
        # استفاده از TIMEOUT کلی برای دریافت لیست اولیه URL ها
        res = requests.get(website, timeout=TIMEOUT, headers=headers, allow_redirects=True)
        res.raise_for_status() # Check for HTTP errors (4xx or 5xx)

        # تعیین انکودینگ صحیح
        res.encoding = res.apparent_encoding

        soup = BeautifulSoup(res.text, DEFAULT_PARSER)

        # افزودن خود URL اصلی به لیست چون ممکن است در لینک‌ها نباشد
        urls.add(website.strip('/')) # اضافه کردن URL پایه

        for a in soup.select('a[href]'):
            try:
                href = a['href']
                # ساخت URL کامل و معتبرسازی اولیه
                full_url = urljoin(website, href).split('#')[0] # حذف fragment

                # فقط URL هایی که با دامنه اصلی شروع می شوند و http/https هستند
                if full_url.startswith(website.strip('/')) and validators.url(full_url):
                     urls.add(full_url)
                # جلوگیری از اضافه کردن بی‌نهایت URL
                if len(urls) >= MAX_URLS * 2: # کمی بیشتر جمع کن تا انتخاب بهتری داشته باشی
                    break
            except Exception as link_err:
                 logger.warning(f"Could not process link {a.get('href', 'N/A')} on {website}: {link_err}")
                 continue

    except requests.Timeout:
         logger.error(f"Timeout error fetching URLs from {website} after {TIMEOUT} seconds.")
         # در صورت تایم‌اوت، حداقل خود سایت اصلی رو برگردونیم اگر بقیه نشد
         urls.add(website.strip('/'))
    except requests.RequestException as e:
        logger.error(f"Error fetching URLs from {website}: {e}")
        # در صورت خطا، حداقل خود سایت اصلی رو برگردونیم اگر بقیه نشد
        urls.add(website.strip('/'))
    except Exception as e:
         logger.error(f"Unexpected error during URL fetching from {website}: {e}")
         urls.add(website.strip('/'))


    final_urls = list(urls)
    logger.info(f"Found {len(final_urls)} unique URLs initially for {website}. Limiting to {MAX_URLS}.")
    # بازگرداندن تعداد محدودی URL
    return final_urls[:MAX_URLS]

def check_keyword(keyword, urls_to_check, website_base_url):
    """Check keyword presence with optimized scoring and better logging"""
    keyword_original = keyword # حفظ حالت اصلی برای نمایش
    keyword = str(keyword).lower().strip() # تبدیل به رشته و حالت استاندارد

    if not keyword:
        logger.warning("Empty keyword received.")
        return {"keyword": keyword_original, "found": False, "url": "-", "score": 0, "status": "Empty Keyword"}

    # ایجاد تغییرات کلمه کلیدی
    variations = {
        'exact': keyword,
        'dash': keyword.replace(" ", "-"),
        'nospace': keyword.replace(" ", "")
    }
    best_score = 0
    best_url = "-"
    status = "Not Found" # وضعیت پیش‌فرض

    logger.debug(f"Checking keyword '{keyword}' on {len(urls_to_check)} URLs for {website_base_url}")

    for url in urls_to_check:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (compatible; KeywordCheckerBot/1.0)'}
            # استفاده از تایم‌اوت کوتاه‌تر برای هر URL
            res = requests.get(url, timeout=TIMEOUT_PER_URL, headers=headers, allow_redirects=True)

            if res.status_code != 200:
                logger.warning(f"Got status code {res.status_code} for URL: {url} while checking keyword '{keyword}'")
                continue # برو سراغ URL بعدی

            # تعیین انکودینگ صحیح
            res.encoding = res.apparent_encoding
            content = res.text.lower() # محتوا به حروف کوچک برای مقایسه غیر حساس به حروف
            score = 0

            # --- Optimized scoring ---
            url_lower = url.lower()

            # امتیاز برای وجود در URL
            if variations['exact'] in url_lower:
                score += 2
                logger.debug(f"Keyword '{keyword}' variation 'exact' found in URL: {url}")
            # بررسی سایر تغییرات در URL
            elif any(var in url_lower for var in (variations['dash'], variations['nospace'])):
                 score += 1
                 logger.debug(f"Keyword '{keyword}' variation 'dash/nospace' found in URL: {url}")


            # تجزیه محتوا برای تگ‌های خاص
            soup = BeautifulSoup(content, DEFAULT_PARSER)
            title_tag = soup.find('title')
            title = title_tag.string.lower() if title_tag and title_tag.string else ''

            h1_tag = soup.find('h1')
            h1 = h1_tag.string.lower() if h1_tag and h1_tag.string else ''
            # TODO: Consider checking all H1s? soup.find_all('h1')

            # امتیاز برای وجود در تگ Title (دقیق)
            if variations['exact'] in title:
                score += 3
                logger.debug(f"Keyword '{keyword}' variation 'exact' found in Title: {title} on URL: {url}")

            # امتیاز برای وجود در تگ H1 (دقیق)
            if variations['exact'] in h1:
                score += 4
                logger.debug(f"Keyword '{keyword}' variation 'exact' found in H1: {h1} on URL: {url}")

             # امتیاز برای وجود در کل محتوای صفحه (دقیق)
             # این بررسی سنگین‌تر است، می‌توان آن را در انتها انجام داد
            if variations['exact'] in content:
                score += 5
                logger.debug(f"Keyword '{keyword}' variation 'exact' found in content body of URL: {url}")


            # به‌روزرسانی بهترین امتیاز و URL
            if score > best_score:
                best_score = score
                best_url = url
                status = "Found" # وضعیت به "یافت شد" تغییر می‌کند
                logger.info(f"New best score {best_score} for keyword '{keyword}' found at URL: {url}")

        except requests.Timeout:
             logger.warning(f"Timeout error checking URL: {url} for keyword '{keyword}' after {TIMEOUT_PER_URL} seconds.")
             # اگر بهترین امتیاز تا اینجا صفر بوده، وضعیت را به‌روز کن
             if best_score == 0: status = "Timeout on check"
             continue # برو سراغ URL بعدی
        except requests.RequestException as e:
            logger.warning(f"Request error checking URL: {url} for keyword '{keyword}': {e}")
            if best_score == 0: status = "Request Error on check"
            continue # برو سراغ URL بعدی
        except Exception as e:
             logger.error(f"Unexpected error checking URL {url} for keyword '{keyword}': {e}")
             if best_score == 0: status = "Processing Error"
             continue

    # اگر بهترین امتیاز همچنان صفر است و وضعیت پیش‌فرض است، یعنی واقعا یافت نشد
    if best_score == 0 and status == "Not Found":
         logger.info(f"Keyword '{keyword}' was not found in any of the checked URLs for {website_base_url}")


    return {"keyword": keyword_original, "found": best_score > 0, "url": best_url, "score": best_score, "status": status}

# --- Flask Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    results = []
    download_filename = None
    error = None
    website_url_ L = None

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

        # --- Secure file handling ---
        original_filename = secure_filename(file.filename)
        # استفاده از uuid برای نام فایل موقت آپلود شده
        temp_id = uuid.uuid4()
        temp_filename = f"{temp_id}_{original_filename}"
        temp_path = os.path.join(app.config['INSTANCE_FOLDER'], temp_filename)

        # --- Ensure instance folder exists ---
        os.makedirs(app.config['INSTANCE_FOLDER'], exist_ok=True)


        try:
            file.save(temp_path)
            logger.info(f"File '{original_filename}' uploaded successfully and saved as '{temp_filename}'.")

            # خواندن کلمات کلیدی از ستون اول فایل اکسل
            df = pd.read_excel(temp_path)
            if df.empty or df.shape[1] == 0:
                 error = "فایل اکسل خالی است یا ستونی ندارد."
                 raise ValueError(error) # پرتاب خطا برای رفتن به بلوک finally

            # اطمینان از گرفتن ستون اول حتی اگر نام نداشته باشد
            keywords = df.iloc[:, 0].dropna().astype(str).tolist()
            if not keywords:
                 error = "ستون اول فایل اکسل کلمه کلیدی معتبری ندارد."
                 raise ValueError(error) # پرتاب خطا برای رفتن به بلوک finally

            logger.info(f"Extracted {len(keywords)} keywords to check.")

            # گرفتن URL های وب‌سایت (با استفاده از کش)
            urls_to_check = get_urls(website_url)
            if not urls_to_check:
                 # اگر get_urls لیست خالی برگرداند (مثلاً URL نامعتبر بود)
                 error = f"نمی‌توان URL ها را از {website_url} استخراج کرد. لطفاً آدرس را بررسی کنید."
                 raise ValueError(error)

            logger.info(f"Starting keyword check process on {len(urls_to_check)} URLs using {MAX_WORKERS} workers.")

            # اجرای بررسی کلمات کلیدی به صورت موازی
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                 # ارسال لیست URL ها و آدرس پایه به تابع check_keyword
                 results = list(executor.map(lambda k: check_keyword(k, urls_to_check, website_url), keywords))

            logger.info("Keyword checking process completed.")

            # --- Generate unique output filename ---
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            output_filename = f"results_{secure_filename(website_url.replace('https://','').replace('http://','').split('/')[0])}_{timestamp}.xlsx"
            output_path = os.path.join(app.config['INSTANCE_FOLDER'], output_filename)

            # ذخیره نتایج در فایل اکسل جدید
            pd.DataFrame(results).to_excel(output_path, index=False)
            logger.info(f"Results saved to '{output_filename}'.")
            download_filename = output_filename # نام فایل برای لینک دانلود

        except FileNotFoundError:
             logger.error(f"Error reading uploaded file at {temp_path}. It might have been removed prematurely.")
             error = "خطا در خواندن فایل آپلود شده."
        except ValueError as ve: # خطاهای مربوط به محتوای فایل اکسل
             logger.error(f"Value error during processing: {ve}")
             error = str(ve) # نمایش پیام خطا به کاربر
        except Exception as e:
            logger.exception(f"An unexpected error occurred during processing for website '{website_url}'")
            error = "یک خطای پیش‌بینی نشده در حین پردازش رخ داد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
        finally:
            # پاک کردن فایل آپلود شده موقت در هر صورت
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                    logger.info(f"Temporary uploaded file '{temp_filename}' removed.")
                except OSError as oe:
                    logger.error(f"Error removing temporary file {temp_path}: {oe}")


    # ارسال نتایج، لینک دانلود و خطا به قالب
    return render_template("index.html",
                           results=results,
                           download_filename=download_filename,
                           error=error,
                           website=website_url, # ارسال وبسایت برای نمایش مجدد در فرم
                           max_urls=MAX_URLS) # ارسال محدودیت URL به قالب


@app.route('/download/<filename>')
def download(filename):
    # اعتبارسنجی نام فایل برای امنیت بیشتر
    safe_filename = secure_filename(filename)
    if safe_filename != filename: # اگر نام فایل تغییر کرده، احتمالاً تلاش برای حمله بوده
        logger.warning(f"Attempt to download potentially unsafe filename: {filename}")
        abort(404)

    output_path = os.path.join(app.config['INSTANCE_FOLDER'], safe_filename)
    logger.info(f"Download request for file: {safe_filename}")

    if not os.path.exists(output_path):
        logger.error(f"Download failed. File not found: {output_path}")
        abort(404) # فایل وجود ندارد

    try:
        # ارسال فایل برای دانلود
        # as_attachment=True باعث می‌شود مرورگر دیالوگ دانلود را نشان دهد
        # download_name=safe_filename نام پیشنهادی برای فایل دانلودی است
        return send_file(output_path, as_attachment=True, download_name=safe_filename)
    except Exception as e:
        logger.exception(f"Error sending file {safe_filename} for download.")
        abort(500) # خطای داخلی سرور
    # توجه: فایل دیگر در اینجا حذف نمی‌شود تا از دانلود ناقص جلوگیری شود.
    # شما باید یک راهکار جداگانه برای پاکسازی پوشه instance داشته باشید.
    # مثلاً یک اسکریپت که فایل‌های قدیمی‌تر از X روز را پاک کند.


# --- Main Execution ---
if __name__ == '__main__':
    # ایجاد پوشه instance اگر وجود نداشته باشد
    os.makedirs(app.config['INSTANCE_FOLDER'], exist_ok=True)
    logger.info(f"Instance folder set to: {app.config['INSTANCE_FOLDER']}")
    # اجرای برنامه Flask
    # برای production، از app.run استفاده نکنید.
    # از یک سرور WSGI مثل Gunicorn استفاده کنید:
    # gunicorn --workers 4 --bind 0.0.0.0:5000 app:app
    # (app:app به معنی فایل app.py و آبجکت app درون آن است)
    app.run(host='0.0.0.0', port=5000, debug=False) # debug=False برای محیط شبیه production