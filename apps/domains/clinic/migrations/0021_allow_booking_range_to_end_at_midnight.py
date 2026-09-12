import datetime

from django.db import migrations, models


ACADEMY_MIGRATION_PHASE = "contract"
ACADEMY_MIGRATION_REASON = (
    "시간 범위 예약이 당일 운영의 정확한 자정 종료를 저장할 수 있도록 기존 순서 제약만 좁게 확장한다."
)


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0020_session_booking_interval_minutes_and_more"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="sessionparticipant",
            name="clinic_participant_booking_range_order",
        ),
        migrations.AddConstraint(
            model_name="sessionparticipant",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(booking_start_time__isnull=True)
                    | models.Q(booking_start_time__lt=models.F("booking_end_time"))
                    | models.Q(
                        booking_start_time__gt=datetime.time(0, 0),
                        booking_end_time=datetime.time(0, 0),
                    )
                ),
                name="clinic_participant_booking_range_order",
            ),
        ),
    ]
