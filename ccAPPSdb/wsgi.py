"""
Configuration for ccAPPS django WSGI web application.
This is used by the different WSGI deployment options:
  - mod_wsgi on apache web server.
    See https://docs.djangoproject.com/en/4.2/howto/deployment/wsgi/
  - django development server 'ccAPPSctl.py runserver'
"""

import os
import sys

# Assure ccAPPS is found in the Python path.
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

os.environ["LC_ALL"] = "en_US.UTF-8"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ccAPPSdb.settings")

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()

if "CCAPPS_TEST" not in os.environ:
    from ccAPPSdb.execute.management.commands.scheduletasks import scheduler

    scheduler.start()
