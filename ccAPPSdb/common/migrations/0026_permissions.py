from django.db import migrations


def remove_permissions(apps, _unused):
    Permission = apps.get_model("auth.Permission")
    Permission.objects.filter(
        codename__in=(
            "add_follower",
            "change_follower",
            "view_follower",
            "delete_follower",
            "add_scenario",
            "change_scenario",
            "delete_scenario",
            "view_scenario",
        )
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("common", "0025_user_horizonbefore")]

    operations = [migrations.RunPython(remove_permissions)]
