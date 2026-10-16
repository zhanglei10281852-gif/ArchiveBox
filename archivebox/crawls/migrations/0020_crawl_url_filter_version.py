from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crawls", "0019_crawlschedule_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="crawl",
            name="url_filter_version",
            field=models.PositiveBigIntegerField(blank=True, default=0, null=False),
        ),
    ]
