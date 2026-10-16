import archivebox.base_models.models
import archivebox.uuid_compat
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0060_snapshot_status_modified_idx'),
        ('crawls', '0020_crawl_permissions_version'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    def hydrate_explicit_snapshot_overrides(apps, schema_editor):
        # Rows that already diverged from their crawl's permission were, in
        # practice, explicit per-snapshot choices (the old propagation only
        # touched rows matching the parent value). Preserve that protection
        # when introducing the explicit override marker.
        Snapshot = apps.get_model('core', 'Snapshot')
        Snapshot.objects.exclude(permissions=models.F('crawl__permissions')).update(permissions_override=True)

    operations = [
        migrations.AddField(
            model_name='snapshot',
            name='permissions_override',
            field=models.BooleanField(db_default=False, db_index=True, default=False, editable=False, help_text="Set when an admin explicitly changed this snapshot's visibility; parent publications skip it."),
        ),
        migrations.AddField(
            model_name='snapshot',
            name='permissions_version',
            field=models.PositiveIntegerField(db_default=1, default=1, editable=False, help_text='Version of the permission intent this row currently carries (crawl version when inherited).'),
        ),
        migrations.CreateModel(
            name='PermissionPublish',
            fields=[
                ('id', models.UUIDField(default=archivebox.uuid_compat.uuid7, editable=False, primary_key=True, serialize=False, unique=True)),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('modified_at', models.DateTimeField(auto_now=True)),
                ('scope_type', models.CharField(choices=[('crawl', 'Crawl'), ('snapshot', 'Snapshot')], db_index=True, max_length=16)),
                ('old_permissions', models.CharField(max_length=16)),
                ('new_permissions', models.CharField(max_length=16)),
                ('version', models.PositiveIntegerField(db_index=True)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed'), ('superseded', 'Superseded')], db_index=True, default='pending', max_length=16)),
                ('phase', models.CharField(choices=[('rows', 'Rows'), ('static', 'Static'), ('completed', 'Completed')], default='rows', max_length=16)),
                ('cursor', models.CharField(blank=True, max_length=64, null=True)),
                ('snapshot_count', models.PositiveIntegerField(default=0)),
                ('processed_count', models.PositiveIntegerField(default=0)),
                ('last_error', models.TextField(blank=True, default='')),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('crawl', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='permission_publish_set', to='crawls.crawl')),
                ('created_by', models.ForeignKey(default=archivebox.base_models.models.get_or_create_system_user_pk, on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
                ('snapshot', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='permission_publish_set', to='core.snapshot')),
            ],
            options={
                'verbose_name': 'Permission Publication',
                'verbose_name_plural': 'Permission Publications',
                'abstract': False,
                'indexes': [models.Index(fields=['status', 'scope_type', 'created_at'], name='perm_publish_status_idx'), models.Index(fields=['crawl', 'version'], name='perm_publish_crawl_ver_idx'), models.Index(fields=['snapshot', 'version'], name='perm_publish_snap_ver_idx')],
            },
        ),
        migrations.RunPython(hydrate_explicit_snapshot_overrides, migrations.RunPython.noop),
    ]
