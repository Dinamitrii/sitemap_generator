import os
import io
import secrets
from flask import Flask, render_template, Response, url_for, redirect, flash, send_file, abort
from flask_sqlalchemy import SQLAlchemy
from flask_sitemap import Sitemap
from datetime import datetime
from dotenv import load_dotenv

# Разширения за уеб форми
from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, FloatField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange


# Функция за автоматично генериране на SECRET_KEY в .env файла
def ensure_secret_key_exists():
    env_path = '.env'
    # Създаваме файла, ако изобщо не съществува
    if not os.path.exists(env_path):
        with open(env_path, 'w') as f:
            f.write("FLASK_DEBUG=True\nDATABASE_URL=sqlite:///site.db\nSERVER_NAME=\n")

    # Проверяваме дали вътре има SECRET_KEY
    with open(env_path, 'r') as f:
        content = f.read()

    if "SECRET_KEY=" not in content:
        # Генериране на сигурен случаен ключ от операционната система
        random_key = secrets.token_hex(32)
        with open(env_path, 'a') as f:
            f.write(f"\nSECRET_KEY={random_key}\n")
        print(f" Генериран е нов защитен SECRET_KEY и е записан в {env_path}")


# Стартиране на проверката и зареждане на .env
ensure_secret_key_exists()
load_dotenv()

app = Flask(__name__)

# Конфигуриране от .env
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///site.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SITEMAP_BLUEPRINT_URL_PREFIX'] = '/'
app.config['SITEMAP_INCLUDE_RULES_WITHOUT_PARAMS'] = True

if os.getenv('SERVER_NAME'):
    app.config['SERVER_NAME'] = os.getenv('SERVER_NAME')

db = SQLAlchemy(app)
ext = Sitemap(app=app)


# --- Модел на Базата Данни ---
class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    changefreq = db.Column(db.String(20), default='weekly')
    priority = db.Column(db.Float, default=0.6)


# --- Дефиниране на Уеб Формата (Flask-WTF) ---
class PostForm(FlaskForm):
    title = StringField('Заглавие на статията', validators=[DataRequired(), Length(max=100)])
    slug = StringField('URL Slug', validators=[DataRequired(), Length(max=100)])
    changefreq = SelectField('Честота на обновяване (Changefreq)', choices=[
        ('always', 'Винаги (always)'),
        ('hourly', 'Ежечасно (hourly)'),
        ('daily', 'Ежедневно (daily)'),
        ('weekly', 'Ежеседмично (weekly)'),
        ('monthly', 'Ежемесечно (monthly)'),
        ('yearly', 'Ежегодно (yearly)'),
        ('never', 'Никога (never)')
    ], default='weekly')
    priority = FloatField('Приоритет (0.0 - 1.0)', default=0.6, validators=[
        DataRequired(), NumberRange(min=0.0, max=1.0)
    ])
    submit = SubmitField('Публикувай статията')


# --- Маршрути ---

@app.route('/', methods=['GET', 'POST'])
def index():
    form = PostForm()

    # Проверка дали формата е попълнена правилно и изпратена
    if form.validate_on_submit():
        # Създаване на нов запис в базата данни
        new_post = Post(
            title=form.title.data,
            slug=form.slug.data.strip().lower().replace(" ", "-"),  # Опростен формат за slug
            changefreq=form.changefreq.data,
            priority=form.priority.data
        )
        try:
            db.session.add(new_post)
            db.session.commit()
            return redirect(url_for('index'))
        except Exception:
            db.session.rollback()
            return "Грешка: Възможно е този URL Slug вече да съществува!"

    posts = Post.query.all()
    return render_template('index.html', posts=posts, form=form)


@app.route('/blog/<string:slug>')
def view_post(slug):
    post = Post.query.filter_by(slug=slug).first_or_404()
    return f"""
    <div style="font-family: Arial; max-width: 600px; margin: 40px auto; line-height: 1.6;">
        <a href="{url_for('index')}">&larr; Назад към началото</a>
        <h1>{post.title}</h1>
        <hr>
        <small style="color: #666;">SEO мета данни: Обновяване: {post.changefreq} | Приоритет: {post.priority}</small>
    </div>
    """


# --- Sitemap & Robots.txt ---
@ext.register_generator
def view_post_generator():
    posts = Post.query.all()
    for post in posts:
        yield ('view_post', {'slug': post.slug}, post.updated_at.strftime('%Y-%m-%d'), post.changefreq, post.priority)


@app.route('/robots.txt')
def robots_txt():
    sitemap_url = url_for('flask_sitemap.sitemap', _external=True)
    content = f"User-agent: *\nAllow: /\n\nSitemap: {sitemap_url}"
    return Response(content, mimetype="text/plain")


# ... останалият ви код (модели, форми, индекси) ...

@app.route('/export/<string:filename>')
def export_file(filename):
    if filename == 'sitemap.xml':
        # 1. Извличаме текущите данни за sitemap
        # Използваме скритото системно име, с което flask_sitemap регистрира маршрута си
        try:
            # Извикваме системния отговор на разширението за sitemap
            sitemap_view = app.view_functions['flask_sitemap.sitemap']
            response = sitemap_view()
            file_data = response.get_data()
            mimetype = 'application/xml'
        except Exception:
            return abort(500, "Грешка при генериране на sitemap.")

    elif filename == 'robots.txt':
        # 2. Извличаме съдържанието за robots.txt директно от нашата функция
        sitemap_url = url_for('flask_sitemap.sitemap', _external=True)
        content = f"User-agent: *\nAllow: /\n\nSitemap: {sitemap_url}"
        file_data = content.encode('utf-8')
        mimetype = 'text/plain'

    else:
        return abort(404)  # Непознат файл

    # Изпращаме файла към потребителя като сваляне (download)
    return send_file(
        io.BytesIO(file_data),
        mimetype=mimetype,
        as_attachment=True,
        download_name=filename
    )


if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Създава празна база данни, ако не съществува

    is_debug = os.getenv('FLASK_DEBUG', 'False').lower() in ['true', '1', 't']
    app.run(debug=is_debug)
