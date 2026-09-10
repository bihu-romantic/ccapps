import os

from django.db import DEFAULT_DB_ALIAS

from ccAPPSdb.common.commands import PlanTaskRegistry, PlanTask


@PlanTaskRegistry.register
class archiveData(PlanTask):
    description = "Archiving of data snapshot"
    sequence = 403

    @classmethod
    def getWeight(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        if "supply" in os.environ and "noexport" not in os.environ:
            return 1
        else:
            return -1

    @classmethod
    def run(cls, database=DEFAULT_DB_ALIAS, **kwargs):
        from django.core import management

        management.call_command("archive", database=database)
