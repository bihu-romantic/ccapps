from ...auth import getWebserviceAuthorization

from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS

from ccAPPSdb import __version__


class Command(BaseCommand):
    help = """
    This command generates an API authentication token for a user.
    """

    requires_system_checks = []

    def get_version(self):
        return __version__

    def add_arguments(self, parser):
        parser.add_argument("user", help="User running the command")
        parser.add_argument(
            "--expiry", help="Validity in days of the token", type=int, default=5
        )
        parser.add_argument(
            "--database",
            action="store",
            dest="database",
            default=DEFAULT_DB_ALIAS,
            help="Specifies the database to use",
        ),

    def handle(self, **options):
        token = getWebserviceAuthorization(
            database=options["database"],
            secret=None,
            user=options["user"],
            exp=options["expiry"] * 86400,
        )
        if options["verbosity"]:
            print(
                "Access token for %s, valid for %s days:"
                % (options["user"], options["expiry"])
            )
        return token
