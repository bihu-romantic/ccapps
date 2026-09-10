from django.conf import settings
from django.core.management.commands.migrate import Command as StdCommand
from django.db import connections
from django.db.utils import DEFAULT_DB_ALIAS

from ccAPPSdb.common.models import Scenario


class Command(StdCommand):
    def create_parser(self, prog_name, subcommand, **kwargs):
        kwargs["conflict_handler"] = "resolve"
        return super().create_parser(prog_name, subcommand, **kwargs)

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--database",
            help="Nominates a database to synchronize. Defaults to all scenarios that are in use.",
        )

    def handle(self, *args, **options):
        db = options["database"]
        if db:
            # Database was specified
            super().handle(*args, **options)
            return

        Scenario.syncWithSettings()
        try:
            with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
                cursor.execute(
                    """
                    select name
                    from common_scenario
                    where status='In use' or name = %s
                    """,
                    (DEFAULT_DB_ALIAS,),
                )
                for i in cursor.fetchall():
                    try:
                        print("Start migrating database %s" % i[0].upper())
                        options["database"] = i[0]
                        super().handle(*args, **options)
                        print("Successfully migrated database %s" % i[0].upper())
                    except Exception as e:
                        print("ERROR migrating database %s: %s" % (i[0].upper(), e))
        except Exception:
            options["database"] = DEFAULT_DB_ALIAS
            super().handle(*args, **options)
