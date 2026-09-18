"""Index rendered configuration history by creation time."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Index the timestamp used by history retention cleanup."""

    dependencies = [
        ("api", "0005_destinationgroup_aclrule_destination_groups"),
    ]

    operations = [
        migrations.AlterField(
            model_name="renderedconfighistory",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, db_index=True),
        ),
    ]
