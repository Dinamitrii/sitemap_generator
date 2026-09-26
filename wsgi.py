"""WSGI entry point за production/hosting среди (gunicorn, uWSGI, PythonAnywhere и др.).

За локална разработка продължавай да ползваш:
    python app.py

За production:
    gunicorn wsgi:app --workers 2 --threads 4 --timeout 60

Самото импортиране на app.py вече автоматично:
  1. Създава таблиците, ако липсват (db.create_all()).
  2. Добавя автоматично липсващи колони към вече съществуваща база
     (sync_schema()) - така при нова версия на кода НЕ се налага ръчно
     изтриване на instance/scans.db или ръчни ALTER TABLE команди.

Тази синхронизация е нарочно ограничена само до ADD COLUMN (виж
sync_schema() в app.py) - безопасна е и не пипа съществуващи данни, но не
замества истински инструмент за миграции (Alembic), ако някога потрябва да
се трият/преименуват колони.
"""
from app import app

# Някои WSGI хостове (PythonAnywhere, mod_wsgi, класически Apache setup-и)
# очакват по конвенция точно името "application" в модула.
application = app

if __name__ == '__main__':
    app.run()