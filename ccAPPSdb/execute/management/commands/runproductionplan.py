from django.utils.translation import gettext_lazy as _

from ccAPPSdb.execute.management.commands.runplan import Command as RunplanCommand


class Command(RunplanCommand):
    title = _("创建生产计划")
    index = 1

    help_url = "command-reference.html#runproductionplan"
