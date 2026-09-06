from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("messaging", "0040_provision_clinic_checkout_notifications"),
    ]

    operations = [
        migrations.AddField(
            model_name="messagetemplate",
            name="retired_at",
            field=models.DateTimeField(
                blank=True,
                help_text="발송 문구 선택에서 영구 제외한 시각. 과거 발송 추적을 위해 행은 보존합니다.",
                null=True,
            ),
        ),
    ]
