from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crawls", "0019_crawlschedule_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="crawl",
            name="permissions_version",
            # db_default keeps historical raw-SQL migration inserts (e.g.
            # core.0024's default-crawl INSERT on 0.8->0.9 upgrade paths)
            # working after this column exists.
            field=models.PositiveIntegerField(db_default=1, default=1, editable=False, help_text="Version of the current PERMISSIONS intent; bumped whenever visibility changes."),
        ),
    ]
