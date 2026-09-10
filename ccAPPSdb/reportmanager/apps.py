from django.apps import AppConfig
from django.core import checks

from ccAPPSdb.common.utils import get_databases


@checks.register(checks.Tags.database)
def check_python_packages(app_configs, **kwargs):
    errors = []
    for db, dbparams in get_databases().items():
        if "SQL_ROLE" not in dbparams:
            errors.append(db)
    return (
        [
            checks.Error(
                "No SQL_ROLE setting configured in djangosettings.py for databases: %s"
                % ", ".join(errors),
                id="reportmanager.E001",
            )
        ]
        if errors
        else []
    )


class ReportManagerConfig(AppConfig):
    name = "ccAPPSdb.reportmanager"
    verbose_name = "reportmanager"
