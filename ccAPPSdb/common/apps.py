import os
from pathlib import Path

from django.apps import AppConfig
from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.core.exceptions import ImproperlyConfigured
from django.utils.autoreload import autoreload_started


def watchDjangoSettings(sender, **kwargs):
    for f in ["djangosettings.py", "localsettings.py", "wsgi.py"]:
        sender.extra_files.add(Path(os.path.join(settings.CCAPPS_CONFIGDIR, f)))


class CommonConfig(AppConfig):
    name = "ccAPPSdb.common"
    verbose_name = "common"

    def ready(self):
        autoreload_started.connect(watchDjangoSettings)

        # Validate all required modules are activated
        missing = []
        required_apps = [
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.messages",
            "django.contrib.staticfiles",
            "ccAPPSdb.boot",
            "ccAPPSdb.input",
            "ccAPPSdb.output",
            "ccAPPSdb.execute",
            "ccAPPSdb.common",
            "ccAPPSdb.archive",
            "django_filters",
            "rest_framework",
            "django.contrib.admin",
        ]
        for app in required_apps:
            if app not in settings.INSTALLED_APPS:
                missing.append(app)
        if missing:
            raise ImproperlyConfigured(
                "Missing required apps in INSTALLED_APPS: %s" % ", ".join(missing)
            )

        user_logged_in.disconnect(dispatch_uid="update_last_login")
