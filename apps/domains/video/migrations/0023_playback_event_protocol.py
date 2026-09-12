import django.db.models.deletion
from django.db import migrations, models


def set_ddl_timeouts(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute("SET LOCAL statement_timeout = '30s'")


class Migration(migrations.Migration):
    atomic = True
    dependencies = [
        ("enrollment", "0002_student_deletion_status_snapshot"),
        ("video", "0022_directvideoentitlement"),
    ]
    operations = [
        migrations.RunPython(set_ddl_timeouts, migrations.RunPython.noop),
        migrations.CreateModel(
            name="VideoPlaybackEventBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("batch_id", models.UUIDField()),
                ("payload_sha256", models.CharField(max_length=64)),
                ("event_count", models.PositiveSmallIntegerField()),
                ("violated_count", models.PositiveSmallIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name="videoplaybacksession", name="event_protocol_version",
            field=models.PositiveSmallIntegerField(db_default=1, default=1),
        ),
        migrations.AddConstraint(
            model_name="videoplaybacksession",
            constraint=models.CheckConstraint(
                condition=models.Q(event_protocol_version__in=[1, 2]), name="playback_event_protocol_valid",
            ),
        ),
        migrations.AddField(
            model_name="videoplaybackeventbatch", name="playback_session",
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="event_batches", to="video.videoplaybacksession"),
        ),
        migrations.AddConstraint(
            model_name="videoplaybackeventbatch",
            constraint=models.UniqueConstraint(fields=("playback_session", "batch_id"), name="playback_batch_identity_unique"),
        ),
        migrations.AddConstraint(
            model_name="videoplaybackeventbatch",
            constraint=models.CheckConstraint(condition=models.Q(event_count__gte=1, event_count__lte=50), name="playback_batch_count_valid"),
        ),
        migrations.AddConstraint(
            model_name="videoplaybackeventbatch",
            constraint=models.CheckConstraint(condition=models.Q(violated_count__lte=models.F("event_count")), name="playback_batch_violations_valid"),
        ),
        migrations.RunPython(migrations.RunPython.noop, set_ddl_timeouts),
    ]
