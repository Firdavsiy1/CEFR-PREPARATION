"""
Celery application configuration for cefr_project.

Start the worker with:
    celery -A cefr_project worker -l info

For production with concurrency and a named queue:
    celery -A cefr_project worker -l info -c 4 -Q celery
"""

import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cefr_project.settings')

app = Celery('cefr_project')

# Read Celery config from Django settings under the CELERY_ namespace.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks.py in every INSTALLED_APP.
app.autodiscover_tasks()
