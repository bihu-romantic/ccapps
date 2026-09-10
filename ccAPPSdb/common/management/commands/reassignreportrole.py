import django.apps
from django.core.management.base import BaseCommand
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.models import Q

from ccAPPSdb import __version__
from ccAPPSdb.common.models import Scenario
from ccAPPSdb.common.utils import get_databases


class Command(BaseCommand):
    help = """
    This command will apply a change of the report user to the database schema.
    """

    requires_system_checks = []

    def get_version(self):
        return __version__

    def handle(self, **options):
        for sc in (
            Scenario.objects.using(DEFAULT_DB_ALIAS)
            .filter(Q(status="In use") | Q(name=DEFAULT_DB_ALIAS))
            .only("name")
        ):
            role = get_databases()[sc.name].get("SQL_ROLE", "report_role")
            if role:
                with connections[sc.name].cursor() as cursor:
                    cursor.execute(
                        "select count(*) from pg_roles where rolname = lower(%s)",
                        (role,),
                    )
                    if not cursor.fetchone()[0]:
                        cursor.execute(
                            "create role %s with login noinherit role current_user"
                            % (role,)
                        )
                    for model in django.apps.apps.get_models():
                        if (
                            getattr(model, "allow_report_manager_access", False)
                            and not model._meta.proxy
                        ):
                            print(
                                f"Grant select on table {model._meta.db_table} to {role}"
                            )
                            cursor.execute(
                                "grant select on table %s to %s"
                                % (model._meta.db_table, role)
                            )
