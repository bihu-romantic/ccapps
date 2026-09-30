import os
import sys
from datetime import datetime, timedelta
import psutil
import shlex
import subprocess
import time
from time import sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS
from django.conf import settings
from django.utils.translation import gettext_lazy as _
from django.utils.encoding import force_str
from django.template import RequestContext
from django.template.loader import render_to_string

import ccAPPSdb.common.commands
from ccAPPSdb.common.middleware import _thread_locals
from ccAPPSdb.common.models import User
from ccAPPSdb.common.report import GridReport
from ccAPPSdb.execute.models import Task
from ccAPPSdb.common.utils import get_databases
from ccAPPSdb import __version__


def reload_webservice(database):
    """Replace the in-memory planning service with the plan just written."""
    if os.environ.get("CCAPPS_WEBSERVICE", "true").lower() == "false":
        return
    from django.core import management

    try:
        management.call_command("stopwebservice", database=database, force=True, wait=False)
    except Exception:
        pass
    env = os.environ.copy()
    env.pop("nowebservice", None)
    env.pop("supply", None)
    env.pop("quote", None)
    env["loadplan"] = "1"
    env["CCAPPS_DATABASE"] = database
    env["PYTHONPATH"] = getattr(settings, "CCAPPS_APP", "/app")
    logdir = getattr(settings, "CCAPPS_LOGDIR", "/tmp")
    os.makedirs(logdir, exist_ok=True)
    log = open(os.path.join(logdir, f"webservice-{database}.log"), "a")
    subprocess.Popen(
        [sys.executable, ccAPPSdb.common.commands.__file__],
        env=env,
        cwd=getattr(settings, "CCAPPS_APP", "/app"),
        start_new_session=True,
        stdout=log,
        stderr=subprocess.STDOUT,
    )


def parseConstraints(val):
    try:
        cstrnts = int(val)
    except Exception:
        cstrnts = 0
        for v in val.split(","):
            c = v.strip().lower()
            if c == "capa":
                cstrnts += 4
            elif c == "lt":
                cstrnts += 16 + 32
            elif c == "mfg_lt":
                cstrnts += 16
            elif c == "po_lt":
                cstrnts += 32
    return cstrnts


def constraintString(val):
    if val == 0:
        return "0"
    c = []
    if val & 4:
        c.append("capa")
    if val & 16 or val & 1:
        c.append("mfg_lt")
    if val & 32 or val & 1:
        c.append("po_lt")
    return ",".join(c)


class Command(BaseCommand):
    help = "Runs ccAPPS to generate a plan"

    requires_system_checks = []

    def get_version(self):
        return __version__

    @staticmethod
    def get_python_command():
        venv = os.path.join(settings.CCAPPS_APP, "venv")
        candidates = [
            os.path.join(venv, "bin", "python3"),
            os.path.join(venv, "bin", "python"),
            sys.executable,
            "python3",
            "python",
        ]
        for candidate in candidates:
            if candidate and os.path.isabs(candidate):
                if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                    return candidate
            elif candidate:
                return candidate
        return "python3"

    @staticmethod
    def should_retry_background_task(task, exit_code):
        if exit_code != -11 or not task:
            return False
        args = task.arguments or ""
        return "--env=supply" in args and "--background" in args

    @staticmethod
    def process_exists(pid):
        if not pid:
            return False
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    def add_arguments(self, parser):
        parser.add_argument("--user", dest="user", help="User running the command")
        parser.add_argument(
            "--constraint",
            dest="constraint",
            type=str,
            default="capa,mfg_lt,po_lt,fence",
            help="Constraints to be considered: capa, mfg_lt, po_lt, fence, lt",
        )
        parser.add_argument(
            "--plantype",
            dest="plantype",
            type=int,
            choices=[1, 2],
            default=1,
            help="Plan type: 1=constrained, 2=unconstrained",
        )
        parser.add_argument(
            "--database",
            dest="database",
            default=DEFAULT_DB_ALIAS,
            help="Nominates a specific database to load data from and export results into",
        )
        parser.add_argument(
            "--task",
            dest="task",
            type=int,
            help="Task identifier (generated automatically if not provided)",
        )
        parser.add_argument(
            "--env",
            dest="env",
            help="A comma separated list of extra settings passed as environment variables to the engine",
        )
        parser.add_argument(
            "--background",
            dest="background",
            action="store_true",
            default=False,
            help="Run the planning engine in the background (default = False)",
        )
        parser.add_argument(
            "--daemon",
            dest="daemon",
            action="store_true",
            default=False,
            help="Run the planning engine as a daemon process for which we don't need to wait (default = False)",
        )
        if "ccAPPSdb.odoo" in settings.INSTALLED_APPS:
            parser.add_argument(
                "--odoo_folder",
                dest="odoo_folder",
                default=None,
                help="A folder where to keep odoo data files",
            )

    def handle(self, **options):
        # Set the server timezone to the TIME_ZONE parameter of djangosettings
        os.environ["TZ"] = settings.TIME_ZONE
        time.tzset()

        # Keep all engine-specific settings local to the child process. This
        # command can run inside a long-lived web worker, where changing the
        # process environment (in particular repeatedly extending PATH) would
        # accumulate across requests and eventually make subprocess creation
        # fail with E2BIG.
        child_env = os.environ.copy()

        # Pick up the options
        now = datetime.now()

        if "database" in options:
            database = options["database"] or DEFAULT_DB_ALIAS
        else:
            database = DEFAULT_DB_ALIAS
        if database not in get_databases():
            raise CommandError("No database settings known for '%s'" % database)
        if "user" in options and options["user"]:
            try:
                user = User.objects.all().using(database).get(username=options["user"])
            except Exception:
                raise CommandError("User '%s' not found" % options["user"])
        else:
            user = None

        timestamp = now.strftime("%Y%m%d%H%M%S")
        if database == DEFAULT_DB_ALIAS:
            logfile = "ccAPPS-%s%s.log" % (
                (
                    ""
                    if "partition" not in options
                    else "partition%s-" % options["partition"]
                ),
                timestamp,
            )
        else:
            logfile = "CCAPPS_%s-%s%s.log" % (
                database,
                (
                    ""
                    if "partition" not in options
                    else "partition%s-" % options["partition"]
                ),
                timestamp,
            )

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
                    or task.name not in ("runplan", "odoo_import", "odoo_export")
                ):
                    raise CommandError("Invalid task identifier")
                task.status = "0%"
                task.started = now
                task.logfile = logfile
            else:
                task = Task(
                    name="runplan",
                    submitted=now,
                    started=now,
                    status="0%",
                    user=user,
                    logfile=logfile,
                )

            # Validate options
            if "constraint" in options:
                constraint = parseConstraints(options["constraint"])
            else:
                constraint = 4 + 16 + 32
            if "plantype" in options:
                plantype = int(options["plantype"])
            else:
                plantype = 1

            # Reset environment variables
            for label in ccAPPSdb.common.commands.PlanTaskRegistry.getLabels(
                database=database
            ):
                if "env" in options:
                    # Options specified
                    if label[0] in child_env:
                        del child_env[label[0]]
                else:
                    # No options specified - default to activate them all
                    child_env[label[0]] = "1"
            if "loadplan" in child_env:
                del child_env["loadplan"]
            if "odoo_folder" in child_env:
                del child_env["odoo_folder"]
            for i in range(5):
                v = f"odoo_read_{i}"
                if v in child_env:
                    del child_env[v]

            # Set environment variables
            if task.name != "odoo_import":
                if options["env"]:
                    task.arguments = "--constraint=%s --plantype=%d --env=%s" % (
                        constraintString(constraint),
                        plantype,
                        options["env"],
                    )
                    for i in options["env"].split(","):
                        j = i.split("=")
                        if len(j) == 1:
                            child_env[j[0]] = "1"
                        else:
                            child_env[j[0]] = j[1]
                else:
                    task.arguments = "--constraint=%s --plantype=%d" % (
                        constraintString(constraint),
                        plantype,
                    )
            if options["background"]:
                task.arguments += " --background"
            if options["daemon"]:
                task.arguments += " --daemon"
            if "cluster" in options:
                task.arguments += " --cluster=%s" % options["cluster"]
            if (
                options.get("odoo_folder", None)
                and "ccAPPSdb.odoo" in settings.INSTALLED_APPS
            ):
                task.arguments += f" --odoo_folder={options["odoo_folder"]}"

            # Log task
            # Different from the other tasks the ccAPPS engine will write the processid
            task.save(using=database)

            # Locate commands.py
            cmd = ccAPPSdb.common.commands.__file__

            def setlimits():
                import resource

                if settings.MAXMEMORYSIZE:
                    resource.setrlimit(
                        resource.RLIMIT_AS,
                        (
                            settings.MAXMEMORYSIZE * 1024 * 1024,
                            (settings.MAXMEMORYSIZE + 10) * 1024 * 1024,
                        ),
                    )
                if settings.MAXCPUTIME:
                    resource.setrlimit(
                        resource.RLIMIT_CPU,
                        (settings.MAXCPUTIME, settings.MAXCPUTIME + 5),
                    )

            # Make sure the forecast engine uses the same correct timezone
            child_env["PGTZ"] = settings.TIME_ZONE

            # Prepare environment
            child_env["nowebservice"] = "1"
            if task.name == "odoo_import":
                child_env["odoo_read_1"] = "1"
            child_env["supply"] = "true"
            child_env["quote"] = "true"
            child_env["CCAPPS_PLANTYPE"] = str(plantype)
            child_env["CCAPPS_CONSTRAINT"] = str(constraint)
            child_env["CCAPPS_TASKID"] = str(task.id)
            child_env["CCAPPS_DATABASE"] = database
            child_env["CCAPPS_LOGFILE"] = logfile
            child_env["CCAPPS_PROCESSNAME"] = get_databases()[database][
                "NAME"
            ].replace("demo", "")
            # Add the virtual environment's bin directory to PATH so the
            # ccAPPS binary's embedded Python can find Django and other packages.
            venv = os.path.join(settings.CCAPPS_APP, "venv")
            if os.path.isdir(venv):
                child_env["VIRTUAL_ENV"] = venv
                child_env["PATH"] = (
                    os.path.join(venv, "bin")
                    + os.pathsep
                    + settings.CCAPPS_HOME
                    + os.pathsep
                    + child_env["PATH"]
                    + os.pathsep
                    + settings.CCAPPS_APP
                )
            else:
                child_env["PATH"] = (
                    settings.CCAPPS_HOME
                    + os.pathsep
                    + child_env["PATH"]
                    + os.pathsep
                    + settings.CCAPPS_APP
                )
            if (
                options.get("odoo_folder", None)
                and "ccAPPSdb.odoo" in settings.INSTALLED_APPS
            ):
                child_env["ODOO_FOLDER"] = options["odoo_folder"]
            if os.path.isfile(os.path.join(settings.CCAPPS_HOME, "libccAPPS.so")):
                # Prepend the engine directory instead of replacing the whole
                # search path: locally installed engine dependencies (e.g.
                # libxerces/ICU under ~/.local/lib/ccapps) must stay reachable.
                existing_ld_path = child_env.get("LD_LIBRARY_PATH")
                child_env["LD_LIBRARY_PATH"] = (
                    settings.CCAPPS_HOME + os.pathsep + existing_ld_path
                    if existing_ld_path
                    else settings.CCAPPS_HOME
                )
            if "DJANGO_SETTINGS_MODULE" not in child_env:
                child_env["DJANGO_SETTINGS_MODULE"] = "ccAPPSdb.settings"
            child_env["PYTHONPATH"] = os.path.normpath(settings.CCAPPS_APP)

            child_env["PYTHONUNBUFFERED"] = "1"
            child_env.setdefault("PYTHONFAULTHANDLER", "1")

            python_cmd = self.get_python_command()
            child_args = [python_cmd, cmd]
            max_background_attempts = 2 if options["background"] else 1
            child = None
            if options["background"] or options["daemon"]:
                self.stdout.write(
                    f"Launching background planner with interpreter: {python_cmd}"
                )
                child = subprocess.Popen(
                    child_args, preexec_fn=setlimits, env=child_env
                )
            else:
                ret = subprocess.call(
                    child_args, preexec_fn=setlimits, env=child_env
                )
                if ret != 0 and ret != 2:
                    # Return code 0 is a successful run
                    # Return code is 2 is a run cancelled by a user. That's shown in the status field.
                    raise Exception("Failed with exit code %d" % ret)

            if options["background"]:
                # Wait for the background task to be ready
                attempt = 1
                while True:
                    sleep(5)
                    t = Task.objects.using(database).get(pk=task.id)
                    if t.status in ["100%", "Canceled", "Failed", "Done"]:
                        break
                    if not self.process_exists(t.processid):
                        # Give the child process one final chance to flush its
                        # terminal task state to the database before declaring
                        # this background run failed.
                        sleep(2)
                        t = Task.objects.using(database).get(pk=task.id)
                        if t.status in ["100%", "Canceled", "Failed", "Done"]:
                            break

                        update_fields = ["processid"]
                        t.processid = None
                        t.finished = datetime.now()
                        update_fields.append("finished")
                        exit_code = child.poll() if child else None
                        if (
                            attempt < max_background_attempts
                            and self.should_retry_background_task(t, exit_code)
                        ):
                            attempt += 1
                            retry_note = (
                                "Background planning process crashed with code "
                                f"{exit_code}; retrying attempt {attempt}/{max_background_attempts}"
                            )
                            t.status = "0%"
                            t.message = retry_note
                            t.finished = None
                            update_fields.extend(["status", "message", "finished"])
                            t.save(update_fields=update_fields, using=database)
                            child = subprocess.Popen(
                                child_args, preexec_fn=setlimits, env=child_env
                            )
                            continue
                        t.status = "Failed"
                        if not t.message or t.message == "Generate supply plan":
                            if exit_code is None:
                                t.message = (
                                    "Background planning process ended before "
                                    "updating the final task status "
                                    f"(cmd: {' '.join(child_args)})"
                                )
                            else:
                                t.message = (
                                    "Background planning process exited with code "
                                    f"{exit_code} before updating the final task status "
                                    f"(cmd: {' '.join(child_args)})"
                                )
                        update_fields.extend(["status", "message"])
                        t.save(update_fields=update_fields, using=database)
                        break
                if t.status in ("Done", "100%"):
                    reload_webservice(database)
            else:
                # Reread the task from the database and update it
                task = Task.objects.all().using(database).get(pk=task.id)
                task.processid = None
                task.status = "Done"
                task.finished = datetime.now()
                task.save(
                    update_fields=["processid", "status", "finished"], using=database
                )
                reload_webservice(database)

        except Exception as e:
            if task:
                task = Task.objects.all().using(database).get(pk=task.id)
                task.status = "Failed"
                task.message = "%s" % e
                task.finished = datetime.now()
                task.processid = None
                task.save(using=database)
            raise e

        finally:
            setattr(_thread_locals, "database", old_thread_locals)

    # accordion template
    title = _("创建计划")
    index = 0

    help_url = "command-reference.html#runplan"

    @staticmethod
    def getHTML(request, widget=False):
        if request.user.has_perm("auth.auth.generate_plan"):
            # Collect optional tasks
            planning_options = ccAPPSdb.common.commands.PlanTaskRegistry.getLabels(
                database=request.database
            )
            plantype = "2"
            constraint = 4 + 16 + 32
            current_options = [i[0] for i in planning_options]
            lastrun = (
                Task.objects.all()
                .using(request.database)
                .filter(name="runplan")
                .exclude(arguments__contains="loadplan")
                .order_by("-id")
                .first()
            )
            if lastrun and lastrun.arguments:
                # Copy all settings from the previous run by this user
                for i in shlex.split(lastrun.arguments or ""):
                    if "=" in i:
                        key, val = i.split("=", 1)
                        key = key.strip("--")
                        if key == "constraint":
                            constraint = parseConstraints(val)
                        elif key == "plantype":
                            plantype = val
                        elif key == "env":
                            try:
                                current_options = val.split(",")
                            except Exception:
                                pass
                offset = GridReport.getTimezoneOffset(request)
                if lastrun.started:
                    lastrun.started += offset
                if lastrun.finished:
                    lastrun.finished += offset
                if lastrun.submitted:
                    lastrun.submitted += offset
            return render_to_string(
                "commands/runplan.html",
                {
                    "planning_options": planning_options,
                    "current_options": current_options,
                    "capa_constrained": constraint & 4,
                    "lt_constrained": constraint & 1,
                    "mfg_lt_constrained": constraint & (16 + 1),
                    "po_lt_constrained": constraint & (32 + 1),
                    "fence_constrained": constraint & 8,
                    "plantype": plantype,
                    "widget": widget,
                    "lastrun": lastrun,
                },
                request=request,
            )
        else:
            return None
