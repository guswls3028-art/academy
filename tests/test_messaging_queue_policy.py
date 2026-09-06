import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.test import override_settings

from apps.domains.messaging.policy import MessagingPolicyError
from apps.domains.messaging.sqs_queue import (
    MessagingSQSQueue,
    build_business_idempotency_key,
)
from apps.domains.messaging.services.manual_delivery_identity import (
    build_content_snapshot_sha256,
)
from apps.domains.messaging.services import enqueue_alimtalk
from apps.worker.messaging_worker.sqs_main import (
    _allowed_common_template_ids,
    _has_valid_worker_tenant_binding,
    _normalize_worker_tenants,
    _video_encoding_block_reason,
    _worker_delivery_identity_error,
    _worker_tenant_binding_error,
)
from apps.domains.messaging.security import (
    verify_delivery_identity_signature,
    verify_tenant_binding_signature,
)
from libs.queue.client import QueueUnavailableError, SQSQueueClient


class _FakeQueueClient:
    def __init__(self):
        self.messages = []

    def send_message(self, *, queue_name: str, message: dict) -> bool:
        self.messages.append(dict(message))
        return True


def test_sqs_queue_count_read_failure_is_unavailable_not_synthetic_depth() -> None:
    client = SQSQueueClient.__new__(SQSQueueClient)
    client.sqs = MagicMock()
    client.sqs.get_queue_url.side_effect = RuntimeError("queue read denied")

    with pytest.raises(QueueUnavailableError, match="Queue counts unavailable"):
        client.get_queue_counts("academy-v1-messaging-queue-dlq")


def test_video_encoding_complete_non_alimtalk_mode_is_blocked() -> None:
    with (
        patch("apps.domains.messaging.policy.is_messaging_disabled", return_value=False),
        patch("apps.domains.messaging.policy.is_messaging_restricted", return_value=False),
        patch("apps.domains.messaging.policy.check_recipient_allowed", return_value=True),
    ):
        with pytest.raises(MessagingPolicyError) as exc:
            enqueue_alimtalk(
                tenant_id=1,
                to="01012345678",
                text="영상 인코딩 완료",
                message_mode="sms",
                event_type="video_encoding_complete",
            )

    assert exc.value.reason == "sms_disabled"


def test_worker_blocks_video_encoding_complete_sms_and_missing_template() -> None:
    assert (
        _video_encoding_block_reason(
            event_type="video_encoding_complete",
            message_mode="sms",
            template_id="KA01VIDEO",
        )
        == "video_encoding_complete_non_alimtalk_blocked"
    )
    assert (
        _video_encoding_block_reason(
            event_type="video_encoding_complete",
            message_mode="alimtalk",
            template_id="",
        )
        == "video_encoding_complete_template_required"
    )
    assert (
        _video_encoding_block_reason(
            event_type="video_encoding_complete",
            message_mode="alimtalk",
            template_id="KA01VIDEO",
        )
        == ""
    )


def test_manual_enqueue_without_occurrence_key_gets_unique_business_key() -> None:
    fake_client = _FakeQueueClient()
    with (
        patch("apps.domains.messaging.sqs_queue.get_queue_client", return_value=fake_client),
        patch(
            "apps.domains.messaging.sqs_queue.uuid4",
            side_effect=[SimpleNamespace(hex="first"), SimpleNamespace(hex="second")],
        ),
    ):
        queue = MessagingSQSQueue()
        assert queue.enqueue(
            tenant_id=1,
            to="01012345678",
            text="첫 번째",
            message_mode="alimtalk",
            event_type="manual_send",
            target_type="student",
            target_id=10,
        )
        assert queue.enqueue(
            tenant_id=1,
            to="01012345678",
            text="두 번째",
            message_mode="alimtalk",
            event_type="manual_send",
            target_type="student",
            target_id=10,
        )

    keys = [m["business_idempotency_key"] for m in fake_client.messages]
    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_business_key_v1_fixed_vector_is_unchanged_by_delivery_identity() -> None:
    assert build_business_idempotency_key(
        tenant_id=1,
        source_tenant_id=2,
        channel="alimtalk",
        event_type="manual_lesson_result",
        target_type="student",
        target_id="9301",
        recipient="01012345678",
        occurrence_key="occ-1",
        template_id="KA01TP260406105458211774JKJ3OU55",
    ) == "662f0bd144af97f08680485be5f300a2b8bcdad1f2f920bed9f5410c0fd4e8b3"


@override_settings(MESSAGING_DELIVERY_IDENTITY_V2_ENFORCED=False)
def test_old_writer_payload_is_accepted_by_new_worker_during_reader_first_rollout() -> None:
    assert _worker_delivery_identity_error(
        {
            "event_type": "manual_lesson_result",
            "template_id": "KA01TP260406105458211774JKJ3OU55",
        }
    ) == ""


@override_settings(MESSAGING_DELIVERY_IDENTITY_V2_ENFORCED=False)
def test_new_writer_keeps_v1_business_key_and_adds_verified_v2_identity() -> None:
    fake_client = _FakeQueueClient()
    replacements = [
        {"key": "학원이름", "value": "테스트학원"},
        {"key": "학생이름", "value": "테스트학생"},
        {"key": "강의명", "value": "수학"},
        {"key": "차시명", "value": "1회차"},
        {"key": "선생님메모", "value": "이번 결과"},
        {"key": "사이트링크", "value": "https://example.test"},
    ]
    text = "이번 결과"
    with patch("apps.domains.messaging.sqs_queue.get_queue_client", return_value=fake_client):
        queue = MessagingSQSQueue(wake_messaging_workers=False)
        assert queue.enqueue(
            tenant_id=1,
            source_tenant_id=2,
            to="01012345678",
            text=text,
            message_mode="alimtalk",
            template_id="KA01TP260406105458211774JKJ3OU55",
            provider_template_type="score",
            provider_template_version="Wy7Z91sBXK",
            provider_template_structure_fingerprint="54f3fb7aca49daaf",
            provider_template_content_fingerprint="a5605726f724dd9b",
            provider_template_header_fingerprint="dbaf4d19b3af2b21",
            content_template_id=12,
            content_template_version="2026-09-06T00:00:00+00:00",
            content_snapshot_sha256=build_content_snapshot_sha256(
                text=text,
                replacements=replacements,
            ),
            alimtalk_replacements=replacements,
            event_type="manual_lesson_result",
            target_type="student",
            target_id=9301,
            occurrence_key="occ-1",
        )

    message = fake_client.messages[0]
    assert message["business_idempotency_key"] == (
        "662f0bd144af97f08680485be5f300a2b8bcdad1f2f920bed9f5410c0fd4e8b3"
    )
    assert message["delivery_identity_version"] == "v2"
    assert verify_delivery_identity_signature(
        message,
        signature=message["delivery_identity_signature"],
    )
    assert _worker_tenant_binding_error(message) == ""
    assert _worker_delivery_identity_error(message) == ""

    tampered = {**message, "provider_template_version": "changed"}
    assert (
        _worker_delivery_identity_error(tampered)
        == "invalid_delivery_identity_signature"
    )


def test_signed_automatic_event_requires_exact_provider_variable_contract() -> None:
    fake_client = _FakeQueueClient()
    replacements = [
        {"key": "학원이름", "value": "테스트학원"},
        {"key": "학생이름", "value": "테스트학생"},
        {"key": "강의명", "value": "수학"},
        {"key": "차시명", "value": "1회차"},
        {"key": "강의날짜", "value": "2026-09-06"},
        {"key": "강의시간", "value": "18:00"},
        {"key": "선생님메모", "value": "입실 안내"},
        {"key": "사이트링크", "value": "https://example.test"},
    ]
    text = "입실 안내"
    with patch("apps.domains.messaging.sqs_queue.get_queue_client", return_value=fake_client):
        queue = MessagingSQSQueue(wake_messaging_workers=False)
        assert queue.enqueue(
            tenant_id=1,
            source_tenant_id=2,
            to="01012345678",
            text=text,
            message_mode="alimtalk",
            template_id="KA01TP260406121126868FGddLmrDFUC",
            provider_template_type="attendance",
            provider_template_version="CXHFcwKdJU",
            provider_template_structure_fingerprint="7f443b87eb8d8c95",
            content_snapshot_sha256=build_content_snapshot_sha256(
                text=text,
                replacements=replacements,
            ),
            alimtalk_replacements=replacements,
            event_type="check_in_complete",
            target_type="student",
            target_id=9301,
            occurrence_key="check-in-1",
        )

    message = fake_client.messages[0]
    assert _worker_delivery_identity_error(message) == ""
    tampered_variables = {
        **message,
        "alimtalk_replacements": replacements[:-1],
    }
    from apps.domains.messaging.security import build_delivery_identity_signature

    tampered_variables["delivery_identity_signature"] = build_delivery_identity_signature(
        tampered_variables
    )
    assert (
        _worker_delivery_identity_error(tampered_variables)
        == "invalid_provider_template_variables"
    )


@pytest.mark.parametrize("message_mode", ["lms", "both", "email"])
def test_queue_rejects_every_explicit_non_alimtalk_mode(message_mode: str) -> None:
    queue = MessagingSQSQueue()
    queue.queue_client = _FakeQueueClient()

    with pytest.raises(MessagingPolicyError) as exc:
        queue.enqueue(
            tenant_id=1,
            to="01012345678",
            text="차단 대상",
            message_mode=message_mode,
        )

    expected = "sms_disabled" if message_mode == "lms" else "unsupported_message_mode"
    assert exc.value.reason == expected
    assert queue.queue_client.messages == []


def test_business_key_includes_source_tenant_for_owner_proxy_sends() -> None:
    fake_client = _FakeQueueClient()
    with patch("apps.domains.messaging.sqs_queue.get_queue_client", return_value=fake_client):
        queue = MessagingSQSQueue()
        common = {
            "tenant_id": 1,
            "to": "01012345678",
            "text": "출결 알림",
            "message_mode": "alimtalk",
            "template_id": "KA01ATTEND",
            "event_type": "check_in_complete",
            "target_type": "student",
            "target_id": 10,
            "occurrence_key": "check_in_complete:session:10",
        }
        assert queue.enqueue(**common, source_tenant_id=2)
        assert queue.enqueue(**common, source_tenant_id=3)

    keys = [m["business_idempotency_key"] for m in fake_client.messages]
    assert len(keys) == 2
    assert keys[0] != keys[1]
    for message in fake_client.messages:
        assert verify_tenant_binding_signature(
            signature=message["tenant_binding_signature"],
            tenant_id=message["tenant_id"],
            source_tenant_id=message["source_tenant_id"],
            business_idempotency_key=message["business_idempotency_key"],
        )


def test_canonical_payload_binds_recipient_and_keeps_privacy_safe_trace() -> None:
    fake_client = _FakeQueueClient()
    with patch("apps.domains.messaging.sqs_queue.get_queue_client", return_value=fake_client):
        queue = MessagingSQSQueue()
        assert queue.enqueue(
            tenant_id=1,
            source_tenant_id=2,
            to="010-1234-5678",
            text="trace",
            message_mode="alimtalk",
            event_type="registration_approved_parent",
            target_type="account",
            target_id="parent:7",
            occurrence_key="dispatch:trace-1",
            origin_type="excel_import",
            origin_id="excel-job-1",
        )

    message = fake_client.messages[0]
    assert message["origin_type"] == "excel_import"
    assert message["origin_id"] == "excel-job-1"
    assert len(message["recipient_fingerprint"]) == 64
    assert "01012345678" not in message["recipient_fingerprint"]
    assert _worker_tenant_binding_error(message) == ""

    tampered = {**message, "to": "01099998888"}
    assert _worker_tenant_binding_error(tampered) == "invalid_business_idempotency_key"


def test_worker_normalizes_raw_tenant_payload_to_common_owner() -> None:
    tenant_id, source_tenant_id = _normalize_worker_tenants(
        3,
        None,
        owner_tenant_id=1,
    )

    assert tenant_id == 1
    assert source_tenant_id == 3


def test_worker_preserves_existing_source_when_payload_is_already_owner() -> None:
    tenant_id, source_tenant_id = _normalize_worker_tenants(
        1,
        3,
        owner_tenant_id=1,
    )

    assert tenant_id == 1
    assert source_tenant_id == 3


def test_worker_rejects_contradictory_legacy_tenant_binding() -> None:
    with pytest.raises(ValueError, match="legacy_business_tenant_mismatch"):
        _normalize_worker_tenants(2, 3, owner_tenant_id=1)


def test_enqueue_rejects_cross_tenant_source_spoof() -> None:
    with pytest.raises(MessagingPolicyError) as exc:
        enqueue_alimtalk(
            tenant_id=2,
            source_tenant_id=3,
            to="01012345678",
            text="spoof",
            message_mode="alimtalk",
        )
    assert exc.value.reason == "business_tenant_mismatch"


def test_worker_manual_template_allowlist_contains_only_registered_unified_ids() -> None:
    allowed = _allowed_common_template_ids("manual_send")
    assert allowed
    assert "STALE-OR-HOSTILE-TEMPLATE" not in allowed


def test_worker_unmapped_event_has_no_database_template_fallback() -> None:
    assert _allowed_common_template_ids("qna_answered") == set()
    assert _allowed_common_template_ids("video_encoding_complete") == set()


def test_worker_requires_signed_binding_for_owner_and_legacy_payloads() -> None:
    unsigned_owner = {
        "tenant_id": 1,
        "business_idempotency_key": "owner-key",
    }
    unsigned_legacy_victim = {
        "tenant_id": 2,
        "business_idempotency_key": "victim-key",
    }

    assert not _has_valid_worker_tenant_binding(unsigned_owner)
    assert not _has_valid_worker_tenant_binding(unsigned_legacy_victim)
    assert _worker_tenant_binding_error(unsigned_owner) == "missing_tenant_binding_signature"
    assert _worker_tenant_binding_error(unsigned_legacy_victim) == "missing_tenant_binding_signature"


def test_worker_rollout_gate_allows_only_missing_not_invalid_signatures(settings) -> None:
    settings.MESSAGING_TENANT_BINDING_ENFORCED = False
    unsigned = {"tenant_id": 1, "business_idempotency_key": "legacy-key"}
    invalid = {
        **unsigned,
        "tenant_binding_signature": "invalid",
    }

    assert _worker_tenant_binding_error(unsigned) == ""
    assert _worker_tenant_binding_error(invalid) == "invalid_tenant_binding_signature"


def test_product_producers_cannot_bypass_durable_messaging_outbox() -> None:
    root = Path(__file__).resolve().parents[1]
    allowed = {
        root / "apps/domains/messaging/scheduled.py",
        root / "apps/domains/messaging/services/queue_service.py",
    }
    offenders = []
    for source_root in (root / "apps", root / "academy"):
        for path in source_root.rglob("*.py"):
            if path in allowed or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"(?<!def )\benqueue_alimtalk\(", text):
                offenders.append(str(path.relative_to(root)))

    assert offenders == []
