"""Localize scenario 1 master data and quote display values to Chinese.

Standalone variant of localize_scenario2_to_chinese.py: adds django.setup() so
it can be run as `python <script>` inside the app container, and targets
DATABASE = "scenario1" (frepple1).
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ccAPPSdb.settings")
django.setup()

import re

from django.db import connections, transaction

from ccAPPSdb.input.models import Customer, Demand, Item, Location, Operation, Resource


DATABASE = "scenario1"


MAPPINGS = {
    Customer: {"Client": "客户"},
    Location: {"factory": "工厂"},
    Item: {
        "All items": "全部物料",
        **{"auxiliary material %02d" % i: "辅料%02d" % i for i in range(1, 11)},
        **{"raw material %02d" % i: "原材料%02d" % i for i in range(1, 11)},
        **{
            "product %02d-%02d" % (line, step): "产品%02d-%02d" % (line, step)
            for line in range(1, 11)
            for step in range(1, 8)
        },
    },
    Resource: {
        **{"Line 1 station %sa" % i: "产线1工位%sa" % i for i in range(1, 2)},
        **{"Line 1 station %sb" % i: "产线1工位%sb" % i for i in range(1, 2)},
        **{"Line 1 station %d" % i: "产线1工位%d" % i for i in range(2, 8)},
        "Operators line 1": "产线1操作人员",
    },
    Operation: {
        "%02d step %02d" % (line, step): "产线%02d工序%02d" % (line, step)
        for line in range(1, 11)
        for step in range(1, 8)
    },
    Demand: {
        **{"Order %02d" % i: "订单%02d" % i for i in range(1, 11)},
        **{"Quote Order %03d" % i: "报价订单%03d" % i for i in range(1, 491)},
    },
}


def existing_map(model, mapping):
    names = set(model.objects.using(DATABASE).filter(name__in=mapping).values_list("name", flat=True))
    return {old: new for old, new in mapping.items() if old in names}


def replace_strings(value, replacements):
    if isinstance(value, dict):
        return {
            replacements.get(key, key): replace_strings(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [replace_strings(item, replacements) for item in value]
    return replacements.get(value, value) if isinstance(value, str) else value


def main():
    maps = {model._meta.db_table: existing_map(model, mapping) for model, mapping in MAPPINGS.items()}
    maps = {table: mapping for table, mapping in maps.items() if mapping}
    replacements = {old: new for mapping in maps.values() for old, new in mapping.items()}
    connection = connections[DATABASE]

    with transaction.atomic(using=DATABASE), connection.cursor() as cursor:
        # This application-level FK was originally non-deferrable, unlike the
        # planning master-data FKs. Make it safely participate in renames.
        cursor.execute(
            "ALTER TABLE quote_history DROP CONSTRAINT IF EXISTS quote_history_demand_id_fkey"
        )
        cursor.execute(
            "ALTER TABLE quote_history ADD CONSTRAINT quote_history_demand_id_fkey "
            "FOREIGN KEY (demand_id) REFERENCES demand(name) ON DELETE CASCADE "
            "DEFERRABLE INITIALLY DEFERRED"
        )
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        # Update every FK that targets a renamed master-data table before renaming it.
        cursor.execute(
            """
            SELECT child.relname, attr.attname, parent.relname
            FROM pg_constraint con
            JOIN pg_class child ON child.oid = con.conrelid
            JOIN pg_class parent ON parent.oid = con.confrelid
            JOIN pg_attribute attr ON attr.attrelid = con.conrelid AND attr.attnum = con.conkey[1]
            WHERE con.contype = 'f' AND array_length(con.conkey, 1) = 1
            """
        )
        for child_table, column, parent_table in cursor.fetchall():
            mapping = maps.get(parent_table)
            if not mapping or not re.fullmatch(r"[a-z_]+", child_table) or not re.fullmatch(r"[a-z_]+", column):
                continue
            values = list(mapping.items())
            cursor.execute(
                "UPDATE %s SET %s = v.new FROM (VALUES %s) AS v(old, new) "
                "WHERE %s = v.old" % (
                    child_table, column, ",".join(["(%s,%s)"] * len(values)), column
                ),
                [value for pair in values for value in pair],
            )
        # quote_history is application-owned history data with a demand FK.
        demand_map = maps.get(Demand._meta.db_table, {})
        if demand_map:
            values = list(demand_map.items())
            cursor.execute(
                "UPDATE quote_history SET demand_id = v.new FROM (VALUES %s) AS v(old, new) "
                "WHERE demand_id = v.old" % ",".join(["(%s,%s)"] * len(values)),
                [value for pair in values for value in pair],
            )
        for table, mapping in maps.items():
            values = list(mapping.items())
            cursor.execute(
                "UPDATE %s SET name = v.new FROM (VALUES %s) AS v(old, new) WHERE name = v.old"
                % (table, ",".join(["(%s,%s)"] * len(values))),
                [value for pair in values for value in pair],
            )

        for demand in Demand.objects.using(DATABASE).exclude(quote=None).iterator(chunk_size=100):
            demand.quote = replace_strings(demand.quote, replacements)
            demand.plan = replace_strings(demand.plan, replacements)
            demand.save(using=DATABASE, update_fields=["quote", "plan"])

    print("localized", {table: len(mapping) for table, mapping in maps.items()})


if __name__ == "__main__":
    main()
