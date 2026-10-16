from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0060_snapshot_status_modified_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="url_filter_version",
            field=models.PositiveBigIntegerField(blank=True, default=0, null=False),
        ),
    ]
