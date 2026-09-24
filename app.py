import os
import io
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
from xml.dom import minidom
from datetime import datetime

from flask import Flask, render_template, Response, url_for, redirect, session, send_file, abort
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired, URL

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'super-secret-key-for-crawler')


# --- Дефиниране на Формата за сканиране ---
class ScanForm(FlaskForm):
    domain_url = StringField('Въведете URL адрес на сайт за обхождане', validators=[DataRequired(), URL()])
    submit = SubmitField('Стартирай сканирането')


# --- Функция за обхождане (Crawler) ---
def crawl_site(start_url):
    domain = urlparse(start_url).netloc
    visited = set()
    to_visit = {start_url}

    # 1. СТРАТЕГИЧЕСКО ДОБАВЯНЕ: Лъжем сайта, че сме истински Google Chrome браузър
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    max_pages = 100

    while to_visit and len(visited) < max_pages:
        current_url = to_visit.pop()
        if current_url not in visited:
            visited.add(current_url)
            try:
                # Подаваме нашите фалшиви браузърни заглавия (headers)
                response = requests.get(current_url, headers=headers, timeout=5)

                # Ако сайтът пак ни блокира, прескачаме страницата
                if response.status_code != 200:
                    continue

                soup = BeautifulSoup(response.text, 'html.parser')
                for a_tag in soup.find_all('a', href=True):
                    full_url = urljoin(current_url, a_tag['href'])
                    parsed_url = urlparse(full_url)

                    # Проверка дали линкът принадлежи на същия домейн
                    if parsed_url.netloc == domain and parsed_url.scheme in ['http', 'https']:
                        # ПОПРАВКА ТУК: Оставяме адреса като нишка (string), а не списък (list)
                        clean_url = full_url.split('#')[0]

                        if clean_url not in visited:
                            to_visit.add(clean_url)
            except Exception as e:
                print(f"Грешка при сканиране на {current_url}: {e}")
                continue

    return list(visited)


# --- Маршрути ---

@app.route('/', methods=['GET', 'POST'])
def index():
    form = ScanForm()
    if form.validate_on_submit():
        target_url = form.domain_url.data.strip('/')

        # Обхождаме сайта и взимаме списък с уникални линкове
        discovered_urls = crawl_site(target_url)

        # Запазваме резултатите временно в потребителската сесия
        session['scanned_domain'] = target_url
        session['discovered_urls'] = discovered_urls
        session['url_count'] = len(discovered_urls)

        return redirect(url_for('index'))

    return render_template('index.html', form=form)


@app.route('/export/<string:filename>')
def export_file(filename):
    # Ако потребителят се опитва да свали файл без първо да е сканирал сайт
    if 'scanned_domain' not in session or 'discovered_urls' not in session:
        return abort(400, "Първо трябва да сканирате уебсайт!")

    target_url = session['scanned_domain']
    urls = session['discovered_urls']
    today = datetime.today().strftime('%Y-%m-%d')

    if filename == 'sitemap.xml':
        root = ET.Element("urlset", xmlns="http://sitemaps.org")
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
        content = f"User-agent: *\nAllow: /\n\nSitemap: {target_url}/sitemap.xml"
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
    app.run(debug=True)
