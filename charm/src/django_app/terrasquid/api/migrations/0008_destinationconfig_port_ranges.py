from django.db import migrations, models

import terrasquid.api.validators


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0007_seed_config_version"),
    ]

    operations = [
        migrations.AlterField(
            model_name="destinationconfig",
            name="ports",
            field=models.JSONField(
                blank=True, default=list, null=True, validators=[terrasquid.api.validators.validate_ports]
            ),
        ),
        migrations.AddField(
            model_name="destinationconfig",
            name="port_ranges",
            field=models.JSONField(
                blank=True, default=None, null=True, validators=[terrasquid.api.validators.validate_port_ranges]
            ),
        ),
    ]
