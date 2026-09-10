edition = "Community Edition"

try:
    import importlib.metadata

    __version__ = importlib.metadata.version("ccAPPSdb")
except Exception:
    __version__ = "development"

VERSION = __version__  # Old custom way, deprecated

# Recognize ASGI vs WSGI mode
mode = "WSGI"


def runCommand(taskname, *args, **kwargs):
    """
    Auxilary method to run a django command. It is intended to be used
    as a target for the multiprocessing module.

    The code is put here, such that a child process loads only
    a minimum of other python modules.
    """
    # Initialize django
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ccAPPSdb.settings")
    import django

    django.setup()

    # Be sure to use the correct database
    from django.conf import settings
    from django.db import DEFAULT_DB_ALIAS, connections
    from ccAPPSdb.common.middleware import _thread_locals
    from ccAPPSdb.common.utils import get_databases

    from threading import local

    database = kwargs.get("database", DEFAULT_DB_ALIAS)
    setattr(_thread_locals, "database", database)
    connections._connections = local()
    if "CCAPPS_TEST" in os.environ:
        settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
        for db in get_databases():
            get_databases()[db]["NAME"] = get_databases()[db]["TEST"]["NAME"]

    # Run the command
    try:
        from django.core import management

        management.call_command(taskname, *args, **kwargs)
    except Exception as e:
        taskid = kwargs.get("task", None)
        if taskid:
            from datetime import datetime
            from ccAPPSdb.execute.models import Task

            task = Task.objects.all().using(database).get(pk=taskid)
            task.status = "Failed"
            now = datetime.now()
            if not task.started:
                task.started = now
            task.finished = now
            task.message = str(e)
            task.processid = None
            task.save(using=database)


def runFunction(func, *args, **kwargs):
    """
    Auxilary method to run the "func".start(*args, **kwargs) method using
    the multiprocessing module.

    The code is put here, such that a child process loads only
    a minimum of other python modules.
    """
    # Initialize django
    import importlib
    import os

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ccAPPSdb.settings")
    import django

    django.setup()

    # Be sure to use the correct database
    from django.conf import settings
    from django.db import DEFAULT_DB_ALIAS, connections
    from ccAPPSdb.common.middleware import _thread_locals
    from ccAPPSdb.common.utils import get_databases

    from threading import local

    database = kwargs.get("database", DEFAULT_DB_ALIAS)
    setattr(_thread_locals, "database", database)
    connections._connections = local()
    if "CCAPPS_TEST" in os.environ:
        settings.EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
        for db in get_databases():
            get_databases()[db]["NAME"] = get_databases()[db]["TEST"]["NAME"]

    # Run the function
    mod_name, func_name = func.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    getattr(mod, func_name).start(*args, **kwargs)
