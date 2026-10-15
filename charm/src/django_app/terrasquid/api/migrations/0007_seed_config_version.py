from django.db import migrations


def seed_config_version(apps, schema_editor):
    apps.get_model("api", "ConfigVersion").objects.using(schema_editor.connection.alias).get_or_create(pk=1)


class Migration(migrations.Migration):
    dependencies = [("api", "0006_renderedconfighistory_created_at_index")]

    operations = [migrations.RunPython(seed_config_version, migrations.RunPython.noop)]
