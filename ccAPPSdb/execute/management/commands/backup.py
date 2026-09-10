import os
import subprocess
from datetime import datetime
import importlib.metadata
import platform

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, connections
from django.utils.translation import gettext_lazy as _
from django.template.loader import render_to_string

from ccAPPSdb.execute.models import Task
from ccAPPSdb.common.middleware import _thread_locals
from ccAPPSdb.common.models import User, Attribute
from ccAPPSdb.common.utils import get_databases, getPostgresVersion
from ccAPPSdb import __version__


class Command(BaseCommand):
    help = """
      This command creates a database dump of the ccAPPS database.

      It also removes dumps older than a month to limit the disk space usage.
      If you want to keep dumps for a longer period of time, you'll need to
      copy the dumps to a different location.

      The pg_dump command needs to be in the path, otherwise this command
      will fail.
      """

    requires_system_checks = []

    def get_version(self):
        return __version__

    def add_arguments(self, parser):
        parser.add_argument("--user", help="User running the command")
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            help="Nominates a specific database to backup",
        )
        parser.add_argument(
            "--task",
            type=int,
            help="Task identifier (generated automatically if not provided)",
        )

    def handle(self, **options):
        # Pick up the options
        database = options["database"]
        if database not in get_databases():
            raise CommandError("No database settings known for '%s'" % database)
        if options["user"]:
            try:
                user = User.objects.all().using(database).get(username=options["user"])
            except Exception:
                raise CommandError("User '%s' not found" % options["user"])
        else:
            user = None

        now = datetime.now()
        task = None
        old_thread_locals = getattr(_thread_locals, "database", None)
        try:
            # Initialize the task
            setattr(_thread_locals, "database", database)
            if "task" in options and options["task"]:
                try:
                    task = Task.objects.all().using(database).get(pk=options["task"])
                except Exception:
                    raise CommandError("Task identifier not found")
                if (
                    task.started
                    or task.finished
                    or task.status != "Waiting"
                    or task.name not in ("CCAPPS_backup", "backup")
                ):
                    raise CommandError("Invalid task identifier")
                task.status = "0%"
                task.started = now
            else:
                task = Task(
                    name="backup", submitted=now, started=now, status="0%", user=user
                )
            task.message = "Backing up the database"
            task.save(using=database)

            # Choose the backup file name
            if __version__ == "development":
                backupfile = now.strftime(
                    "database.%s.%%Y%%m%%d.%%H%%M%%S.dump" % database
                )
            else:
                backupfile = now.strftime(
                    "database_%s.%s.%%Y%%m%%d.%%H%%M%%S.dump" % (__version__, database)
                )
            task.message = "Backup to file %s" % backupfile

            # Copy data managed only in the default database into our scenario
            if database != DEFAULT_DB_ALIAS:
                User.synchronize(database=database)
                Attribute.synchronize(database)

            # Create an dump_info table
            with connections[database].cursor() as cursor:
                cursor.execute(
                    """
                    drop table if exists dump_info;
                    create table dump_info (name varchar (256), value varchar(256));
                    """
                )
                # add ccAPPS version and current time
                cursor.execute(
                    """
                    insert into dump_info values ('ccAPPS version',%s);
                    insert into dump_info select 'time', now();
                    insert into dump_info values ('os version',%s);
                    insert into dump_info values ('python version',%s);
                    """,
                    (__version__, platform.platform(), platform.python_version()),
                )
                # add list of installed apps
                cursor.executemany(
                    "insert into dump_info values ('installed app',%s)",
                    [(i,) for i in settings.INSTALLED_APPS],
                )

                # add list of python modules
                cursor.executemany(
                    "insert into dump_info values ('python module',%s)",
                    [
                        (f"{d.metadata['Name']} {d.version}",)
                        for d in importlib.metadata.distributions()
                    ],
                )

            # Run the backup command
            env = os.environ.copy()
            if get_databases()[database]["PASSWORD"]:
                env["PGPASSWORD"] = get_databases()[database]["PASSWORD"]
            args = [
                f"/usr/lib/postgresql/{getPostgresVersion()}/bin/pg_dump",
                "-Fc",
                "-w",
                "--username=%s" % get_databases()[database]["USER"],
                "--file=%s"
                % os.path.abspath(os.path.join(settings.CCAPPS_LOGDIR, backupfile)),
            ]
            if get_databases()[database]["HOST"]:
                args.append("--host=%s" % get_databases()[database]["HOST"])
            if get_databases()[database]["PORT"]:
                args.append("--port=%s" % get_databases()[database]["PORT"])
            args.append(get_databases()[database]["NAME"])
            with subprocess.Popen(args, env=env) as p:
                try:
                    task.processid = p.pid
                    task.save(using=database)
                    p.wait()
                except Exception:
                    p.kill()
                    p.wait()
                    raise Exception("Run of run pg_dump failed")

            # drop installed apps table
            with connections[database].cursor() as cursor:
                cursor.execute(
                    """
                    drop table if exists dump_info;
                """
                )

            # Task update
            task.logfile = backupfile
            task.processid = None
            task.status = "99%"
            task.save(using=database)

            # Delete backups older than a month
            for f in os.listdir(settings.CCAPPS_LOGDIR):
                if os.path.isfile(os.path.join(settings.CCAPPS_LOGDIR, f)):
                    # Note this is NOT 100% correct on UNIX. st_ctime is not always the creation date...
                    created = datetime.fromtimestamp(
                        os.stat(os.path.join(settings.CCAPPS_LOGDIR, f)).st_ctime
                    )
                    if f.lower().endswith(".dump") and (now - created).days > 31:
                        try:
                            os.remove(os.path.join(settings.CCAPPS_LOGDIR, f))
                        except Exception:
                            pass

            # Task update
            task.status = "Done"
            task.finished = datetime.now()
            task.processid = None

        except Exception as e:
            if task:
                task.status = "Failed"
                task.message = "%s" % e
                task.finished = datetime.now()
                task.processid = None
            raise e

        finally:
            if task:
                task.save(using=database)
            setattr(_thread_locals, "database", old_thread_locals)

    # accordion template
    title = _("Contact ccAPPS support")
    index = 3100

    help_url = "command-reference.html#backup"

    @staticmethod
    def getHTML(request):
        return None
