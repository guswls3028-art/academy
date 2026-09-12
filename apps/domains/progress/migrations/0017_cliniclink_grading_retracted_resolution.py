from django.db import migrations, models


ACADEMY_MIGRATION_PHASE = "contract"
ACADEMY_MIGRATION_REASON = (
    "Register the GRADING_RETRACTED audit choice for stale automatic clinic "
    "projections; the nullable varchar storage remains rolling compatible."
)


class Migration(migrations.Migration):

    dependencies = [
        ("progress", "0016_cliniclink_not_submitted_resolution"),
    ]

    operations = [
        migrations.AlterField(
            model_name="cliniclink",
            name="resolution_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("EXAM_PASS", "시험 통과"),
                    ("HOMEWORK_PASS", "과제 통과"),
                    ("MANUAL_OVERRIDE", "관리자 수동 해소"),
                    ("WAIVED", "면제"),
                    ("CARRIED_OVER", "다음 차수로 이월"),
                    ("SOURCE_REMOVED", "원본 삭제"),
                    ("NOT_SUBMITTED", "시험 미응시 전환"),
                    ("GRADING_RETRACTED", "채점 미완료 투영 철회"),
                    ("BOOKING_LEGACY", "레거시(예약 기반)"),
                ],
                help_text=(
                    "해소 유형: 시험통과/과제통과/수동해소/면제/원본삭제/"
                    "시험미응시/채점미완료투영철회/레거시"
                ),
                max_length=30,
                null=True,
            ),
        ),
    ]
