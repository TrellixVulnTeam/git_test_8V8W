# Generated by Django 2.2.12 on 2021-05-11 16:22

import re

from django.contrib.postgres.aggregates import ArrayAgg
from django.db import migrations

REPLACE_RE = re.compile(r"[^\w.-]")


def fix_ownerdata_keys(apps, schema_editor):
    OwnerData = apps.get_model("maasserver", "OwnerData")

    existing_node_keys = {
        node_id: set(values)
        for node_id, values in OwnerData.objects.values_list(
            "node_id"
        ).annotate(keys=ArrayAgg("key"))
    }

    for entry in OwnerData.objects.exclude(key__regex=r"^[\w.-]+$"):
        orig_key = entry.key

        node_keys = existing_node_keys[entry.node_id]
        node_keys.remove(orig_key)

        new_key = REPLACE_RE.sub("_", entry.key)
        while new_key in node_keys:
            new_key += "_"
        node_keys.add(new_key)

        entry.key = new_key
        entry.save()


class Migration(migrations.Migration):

    dependencies = [
        ("maasserver", "0239_add_iprange_specific_dhcp_snippets"),
    ]

    operations = [migrations.RunPython(fix_ownerdata_keys)]