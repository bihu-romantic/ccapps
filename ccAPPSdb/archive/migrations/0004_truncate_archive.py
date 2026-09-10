from django.db import migrations


class Migration(migrations.Migration):
    """
    The archived inventory table is incorrect in previous releases.
    We're resetting all arhived history till now and restart.
    """

    dependencies = [("archive", "0003_permissions")]
    operations = [migrations.RunSQL("truncate ax_manager cascade")]
