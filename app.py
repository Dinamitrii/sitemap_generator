import io
import ipaddress
import json
import os
import socket
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
from xml.dom import minidom

import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, redirect, render_template, send_file, session, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired, URL, ValidationError

DEBUG = os.getenv('FLASK_DEBUG') == '1'

app = Flask(__name__)

# --- Конфигурация ---
# SECRET_KEY вече няма публична стойност по подразбиране (репото е публично).
# Генериране: python -c "import secrets; print(secrets.token_hex(32))"
_secret_key = os.getenv('SECRET_KEY')
if not _secret_key:
    if DEBUG:
        _secret_key = 'dev-only-insecure-key'
    else:
        raise RuntimeError(
            'Липсва променлива на средата SECRET_KEY. '
            'Задайте я преди стартиране (за локална разработка: FLASK_DEBUG=1).'
        )
app.config['SECRET_KEY'] = _secret_key
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Резултатите се пазят на сървъра (не в бисквитката, която е лимитирана до ~4 KB).
os.makedirs(app.instance_path, exist_ok=True)
_db_url = os.getenv('DATABASE_URL') or 'sqlite:///' + os.path.join(app.instance_path, 'scans.db')
if _db_url.startswith('postgres://'):  # Heroku формат -> SQLAlchemy формат
    _db_url = _db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Настройки на краулъра ---
USER_AGENT = os.getenv(
    'CRAWLER_USER_AGENT',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
)
MAX_PAGES = 100                 # макс. страници в sitemap-а
MAX_REQUESTS = 300              # макс. заявки общо (включва неуспешните)
CRAWL_TIME_LIMIT = 240          # секунди; после се връща каквото е събрано
REQUEST_TIMEOUT = 5
MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_CONCURRENT_SCANS = 3
SCAN_STALE_AFTER = timedelta(minutes=10)   # "висящо" сканиране -> грешка
SCAN_RETENTION = timedelta(hours=24)       # стари резултати се трият
SKIP_EXTENSIONS = (
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.pdf', '.zip', '.rar',
    '.gz', '.mp3', '.mp4', '.avi', '.mov', '.doc', '.docx', '.xls', '.xlsx', '.ppt',
    '.pptx', '.css', '.js', '.xml', '.json', '.txt',
)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Scan(db.Model):
    id = db.Column(db.String(32), primary_key=True)
    start_url = db.Column(db.String(2048), nullable=False)
    base_url = db.Column(db.String(2048))
    status = db.Column(db.String(10), nullable=False, default='running')  # running | done | error
    crawled = db.Column(db.Integer, nullable=False, default=0)
    urls_json = db.Column(db.Text, nullable=False, default='[]')
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    @property
    def urls(self):
        return json.loads(self.urls_json or '[]')


with app.app_context():
    db.create_all()


# --- Защита срещу SSRF ---
def is_public_host(hostname, port=None):
    """True само ако всички адреси на хоста са публични (не вътрешна мрежа).

    Забележка: остава теоретичен риск от DNS rebinding между проверката и
    самата заявка; за пълна защита са нужни изходящ прокси или firewall правила.
    """
    if not hostname:
        return False
    try:
        infos = socket.getaddrinfo(hostname, port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split('%')[0])
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            return False
    return True


# --- Помощни функции за краулъра ---
def site_key(parts):
    """Идентификатор на сайта: хост без 'www.' + порт."""
    host = (parts.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return host, parts.port


def normalize_url(url, scheme, netloc):
    """Единен вид: без фрагмент, без параметри, без краен '/', фиксиран scheme/хост."""
    return f"{scheme}://{netloc}{urlparse(url).path.rstrip('/')}"


def fetch(url, headers, site):
    """GET със сигурно следване на пренасочвания (макс. MAX_REDIRECTS).

    Всеки хоп се проверява дали е публичен и дали е от същия сайт.
    Връща (response, final_url) или None. Викащият трябва да затвори response.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parts = urlparse(current)
        if parts.scheme not in ('http', 'https') or site_key(parts) != site:
            return None
        if not is_public_host(parts.hostname, parts.port):
            return None
        resp = requests.get(current, headers=headers, timeout=REQUEST_TIMEOUT,
                            allow_redirects=False, stream=True)
        if resp.is_redirect:
            location = resp.headers.get('Location')
            resp.close()
            if not location:
                return None
            current = urljoin(current, location)
            continue
        return resp, current
    return None


def read_limited(resp):
    chunks, size = [], 0
    for chunk in resp.iter_content(chunk_size=65536):
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_PAGE_BYTES:
            break
    return b''.join(chunks)


def load_robots(scheme, netloc, headers, site):
    """Зарежда robots.txt на сканирания сайт (при липса/грешка - всичко е позволено).

    Стандартният RobotFileParser не поддържа напълно wildcard правила (* и $).
    """
    parser = RobotFileParser()
    lines = []
    try:
        fetched = fetch(f'{scheme}://{netloc}/robots.txt', headers, site)
        if fetched:
            resp, _ = fetched
            try:
                if resp.status_code == 200:
                    lines = read_limited(resp).decode('utf-8', 'replace').splitlines()
            finally:
                resp.close()
    except requests.RequestException:
        pass
    parser.parse(lines)
    return parser


# --- Функция за обхождане (Crawler) ---
def crawl_site(start_url, on_progress=None):
    """Обхожда сайта в ширина. Връща (base_url, [успешни HTML страници])."""
    headers = {'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml'}
    site = site_key(urlparse(start_url))

    queue = deque([start_url])
    queued = {start_url}
    pages, page_set = [], set()
    scheme = netloc = robots = None
    requests_made = 0
    deadline = time.monotonic() + CRAWL_TIME_LIMIT

    while queue and len(pages) < MAX_PAGES and requests_made < MAX_REQUESTS:
        if time.monotonic() > deadline:
            break
        url = queue.popleft()
        if robots is not None and not robots.can_fetch('*', url):
            continue

        requests_made += 1
        try:
            fetched = fetch(url, headers, site)
            if not fetched:
                continue
            resp, final_url = fetched
            try:
                content_type = resp.headers.get('Content-Type', '').lower()
                if resp.status_code != 200 or 'html' not in content_type:
                    continue
                body = read_limited(resp)
            finally:
                resp.close()
        except requests.RequestException as e:
            app.logger.info('Грешка при сканиране на %s: %s', url, e)
            continue

        final_parts = urlparse(final_url)
        if scheme is None:
            # Каноничен вид (http/https, www/без www) по първата успешна страница.
            scheme, netloc = final_parts.scheme, final_parts.netloc.lower()
            robots = load_robots(scheme, netloc, headers, site)
            if not robots.can_fetch('*', final_url):
                break

        canonical = normalize_url(final_url, scheme, netloc)
        if canonical in page_set:
            continue
        page_set.add(canonical)
        pages.append(canonical)
        if on_progress:
            on_progress(len(pages))

        soup = BeautifulSoup(body, 'html.parser')
        for a_tag in soup.find_all('a', href=True):
            link = urljoin(final_url, a_tag['href'].strip())
            parts = urlparse(link)
            if parts.scheme not in ('http', 'https') or site_key(parts) != site:
                continue
            # Линкове с параметри (?page=, ?sort=) създават безкрайни варианти.
            if parts.query or parts.path.lower().endswith(SKIP_EXTENSIONS):
                continue
            candidate = normalize_url(link, scheme, netloc)
            if candidate not in queued:
                queued.add(candidate)
                queue.append(candidate)

    base = f'{scheme}://{netloc}' if scheme else None
    return base, pages


def run_scan(scan_id):
    """Изпълнява се във фонова нишка."""
    with app.app_context():
        try:
            scan = db.session.get(Scan, scan_id)

            def progress(count):
                scan.crawled = count
                db.session.commit()

            base, urls = crawl_site(scan.start_url, progress)
            if urls:
                scan.base_url = base
                scan.urls_json = json.dumps(urls)
                scan.status = 'done'
            else:
                scan.status = 'error'
                scan.error = ('Не бяха намерени достъпни HTML страници: сайтът блокира '
                              'заявките, изисква вход или robots.txt забранява обхождането.')
            db.session.commit()
        except Exception:
            app.logger.exception('Неуспешно сканиране %s', scan_id)
            db.session.rollback()
            scan = db.session.get(Scan, scan_id)
            if scan:
                scan.status = 'error'
                scan.error = 'Възникна неочаквана грешка при сканирането.'
                db.session.commit()
        finally:
            db.session.remove()


def purge_scans():
    now = utcnow()
    Scan.query.filter(Scan.created_at < now - SCAN_RETENTION).delete()
    Scan.query.filter(Scan.status == 'running', Scan.created_at < now - SCAN_STALE_AFTER).update(
        {'status': 'error', 'error': 'Сканирането отне твърде дълго и беше прекратено.'})
    db.session.commit()


def get_current_scan():
    scan_id = session.get('scan_id')
    if not scan_id:
        return None
    scan = db.session.get(Scan, scan_id)
    if (scan and scan.status == 'running'
            and utcnow() - scan.created_at > SCAN_STALE_AFTER):
        scan.status = 'error'
        scan.error = 'Сканирането отне твърде дълго и беше прекратено.'
        db.session.commit()
    return scan


def add_error(field, message):
    field.errors = list(field.errors) + [message]


# --- Дефиниране на Формата за сканиране ---
class ScanForm(FlaskForm):
    domain_url = StringField('Въведете URL адрес на сайт за обхождане',
                             validators=[DataRequired(), URL()])
    submit = SubmitField('Стартирай сканирането')

    def validate_domain_url(self, field):
        parts = urlparse(field.data.strip())
        if parts.scheme not in ('http', 'https'):
            raise ValidationError('Позволени са само http:// и https:// адреси.')
        if not is_public_host(parts.hostname, parts.port):
            raise ValidationError('Адресът не може да бъде достигнат или сочи към вътрешна мрежа.')


# --- Маршрути ---

@app.route('/', methods=['GET', 'POST'])
def index():
    form = ScanForm()
    if form.validate_on_submit():
        target_url = form.domain_url.data.strip().rstrip('/')
        purge_scans()

        if Scan.query.filter_by(status='running').count() >= MAX_CONCURRENT_SCANS:
            add_error(form.domain_url, 'Сървърът е зает с други сканирания. Опитайте след малко.')
        else:
            scan = Scan(id=uuid.uuid4().hex, start_url=target_url)
            db.session.add(scan)
            db.session.commit()
            session['scan_id'] = scan.id
            session.pop('discovered_urls', None)  # остатък от старата версия
            threading.Thread(target=run_scan, args=(scan.id,), daemon=True).start()
            return redirect(url_for('index'))

    scan = get_current_scan()
    context = {}
    if scan and scan.status == 'running' and not form.errors:
        return render_template('scanning.html', scan=scan, max_pages=MAX_PAGES)
    if scan and scan.status == 'done':
        urls = scan.urls
        session['scanned_domain'] = scan.base_url
        session['url_count'] = len(urls)
        context = {'scanned_domain': scan.base_url, 'discovered_urls': urls,
                   'url_count': len(urls)}
    elif scan and scan.status == 'error':
        add_error(form.domain_url, scan.error)
        session.pop('scan_id', None)

    return render_template('index.html', form=form, **context)


@app.route('/export/<string:filename>')
def export_file(filename):
    # Ако потребителят се опитва да свали файл без първо да е сканирал сайт
    scan = get_current_scan()
    if not scan or scan.status != 'done':
        return abort(400, "Първо трябва да сканирате уебсайт!")

    target_url = scan.base_url
    urls = scan.urls
    today = datetime.today().strftime('%Y-%m-%d')

    if filename == 'sitemap.xml':
        root = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")
        for url in sorted(urls):
            url_tag = ET.SubElement(root, "url")
            loc = ET.SubElement(url_tag, "loc")
            loc.text = url
            lastmod = ET.SubElement(url_tag, "lastmod")
            lastmod.text = today
            changefreq = ET.SubElement(url_tag, "changefreq")
            changefreq.text = "weekly"
            priority = ET.SubElement(url_tag, "priority")
            priority.text = "1.0" if url == target_url else "0.7"

        # Разкрасяване на XML формата
        xml_str = ET.tostring(root, encoding='utf-8')
        parsed_xml = minidom.parseString(xml_str)
        file_data = parsed_xml.toprettyxml(indent="  ", encoding="utf-8")
        mimetype = 'application/xml'

    elif filename == 'robots.txt':
        content = f"User-agent: *\nAllow: /\n\nSitemap: {target_url}/sitemap.xml\n"
        file_data = content.encode('utf-8')
        mimetype = 'text/plain'
    else:
        return abort(404)

    return send_file(
        io.BytesIO(file_data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename
    )


if __name__ == '__main__':
    # Debug режимът е само при FLASK_DEBUG=1 (Werkzeug дебъгерът позволява изпълнение на код).
    app.run(debug=DEBUG)
