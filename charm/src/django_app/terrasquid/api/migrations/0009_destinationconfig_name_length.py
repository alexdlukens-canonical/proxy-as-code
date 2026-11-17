from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("api", "0008_destinationconfig_port_ranges"),
    ]

    operations = [
        migrations.AlterField(
            model_name="destinationconfig",
            name="name",
            field=models.CharField(max_length=255),
        ),
    ]
