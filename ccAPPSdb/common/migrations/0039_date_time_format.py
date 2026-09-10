from django.conf import settings
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("common", "0038_scenario")]

    operations = [
        migrations.RunSQL(
            f"""
            insert into common_parameter (name, value, description, lastmodified)
            select
            'display_time','{str(
                    settings.DATE_STYLE_WITH_HOURS
                    if hasattr(settings, "DATE_STYLE_WITH_HOURS")
                    else False
                ).lower()}','This parameter applies to ALL scenarios and controls if date time fields should display the time. Accepted values: true or false', now()
            from common_parameter
            ON CONFLICT (name) DO NOTHING;
            insert into common_parameter (name, value, description, lastmodified)
            select
            'date_format','{str(
                    settings.DATE_STYLE
                    if hasattr(settings, "DATE_STYLE")
                    else "year-month-day"
                ).lower()}','This parameter applies to ALL scenarios. Accepted values: month-day-year, day-month-year, year-month-day.', now()
            from common_parameter
            ON CONFLICT (name) DO NOTHING;
            """,
            "delete from common_parameter where name in ('display_time','date_format')",
        )
    ]
