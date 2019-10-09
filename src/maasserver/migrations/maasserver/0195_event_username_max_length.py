# -*- coding: utf-8 -*-
# Generated by Django 1.11.11 on 2019-10-08 14:07
from __future__ import unicode_literals

from django.db import (
    migrations,
    models,
)
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('maasserver', '0194_machine_listing_event_index'),
    ]

    operations = [
        migrations.AlterField(
            model_name='event',
            name='node',
            field=models.ForeignKey(editable=False, null=True, on_delete=django.db.models.deletion.SET_NULL, to='maasserver.Node'),
        ),
        migrations.AlterField(
            model_name='event',
            name='username',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
    ]