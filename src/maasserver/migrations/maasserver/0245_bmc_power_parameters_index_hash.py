# Generated by Django 2.2.12 on 2021-08-23 13:54

import django.contrib.postgres.indexes
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("maasserver", "0244_controller_nodes_deployed"),
    ]

    operations = [
        migrations.AlterField(
            model_name="bmc",
            name="power_type",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=10
            ),
        ),
        migrations.AddIndex(
            model_name="bmc",
            index=django.contrib.postgres.indexes.HashIndex(
                fields=["power_parameters"],
                name="maasserver__power_p_511df2_hash",
            ),
        ),
        # replace the unique index with one that uses an hash since the size of
        # power_parameters content might exceed the supported size for the
        # index
        migrations.RunSQL(
            "DROP INDEX maasserver_bmc_power_type_parameters_idx"
        ),
        migrations.RunSQL(
            """
            CREATE UNIQUE INDEX maasserver_bmc_power_type_parameters_idx
            ON maasserver_bmc (power_type, md5(power_parameters::text))
            WHERE (power_type != 'manual')
            """
        ),
    ]