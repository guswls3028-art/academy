from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from apps.worker.messaging_worker.sqs_main import (
    _assert_clinic_reminder_delivery_active,
    _is_non_retryable_send_failure,
    _resolve_tenant_delivery_context,
    _resolve_worker_business_key,
    _safe_payload_shape,
    _send_failure_disposition,
    _should_defer_disabled_tenant_message,
    send_one_alimtalk,
)
from apps.worker.messaging_worker import sqs_main as messaging_worker


def test_recipient_summary_keeps_scoped_operational_phone():
    assert messaging_worker._recipient_summary("최정원", "010-9123-4567") == "최정원 01091234567"


@patch("apps.worker.messaging_worker.sqs_main._get_solapi_client")
@patch("apps.domains.clinic.contracts.is_clinic_booking_reminder_active", return_value=False)
def test_cancelled_range_booking_never_crosses_provider_boundary(active, client):
    payload = {"event_type": "clinic_reminder", "origin_id": "clinic_booking:1:2:20260904:2000"}

    def boundary():
        _assert_clinic_reminder_delivery_active(payload, business_tenant_id=11)
        return True

    result = send_one_alimtalk(
        SimpleNamespace(), to="01012345678", sender="01012345678", pf_id="PF",
        template_id="KA", replacements=[], before_provider_call=boundary,
    )
    client.return_value.send.assert_not_called()
    active.assert_called_once_with(tenant_id=11, origin_id=payload["origin_id"])
    assert result["provider_called"] is False
    assert result["reason"] == "clinic_booking_closed"
    assert _send_failure_disposition(result["reason"], provider_send_started=False) == "terminal"


@patch("apps.domains.clinic.contracts.is_clinic_booking_reminder_active")
def test_fixed_slot_reminder_keeps_existing_provider_contract(active):
    _assert_clinic_reminder_delivery_active(
        {"event_type": "clinic_reminder", "origin_id": "clinic_session:2:reminder"},
        business_tenant_id=11,
    )
    active.assert_not_called()


def test_ppurio_credential_failures_are_non_retryable() -> None:
    assert _is_non_retryable_send_failure("ppurio_not_configured")
    assert _is_non_retryable_send_failure("ppurio_token_rejected")
    assert _is_non_retryable_send_failure("ppurio_token_client_error_401")
    assert _is_non_retryable_send_failure("ppurio_token_failed")
    assert _is_non_retryable_send_failure("invalid_sender_profile_format")


def test_ppurio_provider_unavailable_remains_retryable() -> None:
    assert not _is_non_retryable_send_failure("ppurio_token_unavailable")


def test_provider_call_timeout_is_ambiguous_and_must_not_auto_retry() -> None:
    assert (
        _send_failure_disposition(
            "read timeout",
            provider_send_started=True,
        )
        == "ambiguous"
    )


def test_disabled_tenant_defers_only_required_first_enrollment_notices() -> None:
    assert _should_defer_disabled_tenant_message("registration_approved_student")
    assert _should_defer_disabled_tenant_message("registration_approved_parent")
    assert not _should_defer_disabled_tenant_message("manual_send")


def test_pre_provider_transient_failure_remains_retryable() -> None:
    assert (
        _send_failure_disposition(
            "ppurio_token_unavailable",
            provider_send_started=False,
        )
        == "retry"
    )


def test_terminal_configuration_failure_wins_over_provider_boundary() -> None:
    assert (
        _send_failure_disposition(
            "alimtalk_requires_pf_id_and_template_id",
            provider_send_started=True,
        )
        == "terminal"
    )


def test_explicit_provider_rejection_rolls_back_without_ambiguous_charge() -> None:
    assert (
        _send_failure_disposition(
            "arbitrary provider rejection description",
            provider_send_started=True,
            definitely_not_accepted=True,
            provider_retryable=False,
        )
        == "terminal"
    )


def test_provider_5xx_after_call_is_ambiguous_and_must_not_retry() -> None:
    assert (
        _send_failure_disposition(
            "provider HTTP 503",
            provider_send_started=True,
            definitely_not_accepted=False,
        )
        == "ambiguous"
    )


def test_legacy_payload_gets_deterministic_business_key() -> None:
    payload = {"tenant_id": 1, "to": "01012345678", "text": "same"}

    first = _resolve_worker_business_key(payload, "sqs-legacy")
    second = _resolve_worker_business_key(dict(reversed(list(payload.items()))), "sqs-legacy")

    assert first == second
    assert len(first) == 64


def test_worker_malformed_payload_metadata_excludes_values() -> None:
    shape = _safe_payload_shape(
        {"phone": "01099998888", "secret": "do-not-log-this-value"}
    )

    rendered = str(shape)
    assert shape["payload_type"] == "dict"
    assert shape["keys"] == ["phone", "secret"]
    assert "01099998888" not in rendered
    assert "do-not-log-this-value" not in rendered


@patch(
    "apps.domains.messaging.credit_services.get_tenant_messaging_info",
    return_value=None,
)
def test_worker_tenant_resolution_missing_fails_before_provider(mock_info: MagicMock) -> None:
    try:
        _resolve_tenant_delivery_context(1)
    except RuntimeError as exc:
        assert str(exc) == "provider_tenant_missing_or_inactive"
    else:
        raise AssertionError("missing tenant must fail closed")


@patch(
    "apps.domains.messaging.policy.resolve_kakao_channel",
    return_value={"pf_id": "PF", "use_default": True},
)
@patch(
    "apps.domains.messaging.credit_services.get_tenant_messaging_info",
    side_effect=[
        {
            "tenant_is_active": True,
            "messaging_provider": "solapi",
        },
        None,
    ],
)
def test_worker_inactive_business_source_fails_before_provider(
    mock_info: MagicMock,
    mock_channel: MagicMock,
) -> None:
    try:
        _resolve_tenant_delivery_context(1, source_tenant_id=2)
    except RuntimeError as exc:
        assert str(exc) == "business_tenant_missing_or_inactive"
    else:
        raise AssertionError("inactive business tenant must fail closed")


@patch("apps.worker.messaging_worker.sqs_main._get_solapi_client")
def test_provider_boundary_rejection_prevents_sdk_call(mock_get_client: MagicMock) -> None:
    client = MagicMock()
    mock_get_client.return_value = client
    boundary = MagicMock(return_value=False)

    result = send_one_alimtalk(
        SimpleNamespace(),
        to="01012345678",
        sender="0212345678",
        pf_id="PFID",
        template_id="TEMPLATE",
        before_provider_call=boundary,
    )

    boundary.assert_called_once_with()
    client.send.assert_not_called()
    assert result["reason"] == "provider_boundary_claim_failed"
    assert result["provider_called"] is False


@patch.dict(
    "os.environ",
    {"MESSAGING_RECIPIENT_DENYLIST": "010-9999-8888"},
)
@patch("apps.worker.messaging_worker.sqs_main._get_solapi_client")
def test_recipient_denylist_prevents_provider_call(mock_get_client: MagicMock) -> None:
    client = MagicMock()
    mock_get_client.return_value = client

    result = send_one_alimtalk(
        SimpleNamespace(),
        to="01099998888",
        sender="0212345678",
        pf_id="PFID",
        template_id="TEMPLATE",
    )

    mock_get_client.assert_not_called()
    client.send.assert_not_called()
    assert result["reason"] == "recipient_blocked_by_policy"
    assert result["provider_called"] is False
    assert result["definitely_not_accepted"] is True


@patch("apps.worker.messaging_worker.sqs_main._get_solapi_client")
def test_sdk_timeout_is_explicitly_after_provider_boundary(mock_get_client: MagicMock) -> None:
    client = MagicMock()
    client.send.side_effect = TimeoutError("read timeout")
    mock_get_client.return_value = client
    boundary = MagicMock(return_value=True)

    result = send_one_alimtalk(
        SimpleNamespace(),
        to="01012345678",
        sender="0212345678",
        pf_id="PFID",
        template_id="TEMPLATE",
        before_provider_call=boundary,
    )

    boundary.assert_called_once_with()
    client.send.assert_called_once()
    assert result["provider_called"] is True
    assert _send_failure_disposition(
        result["reason"],
        provider_send_started=result["provider_called"],
    ) == "ambiguous"


@patch("apps.worker.messaging_worker.sqs_main._get_solapi_client")
def test_solapi_balance_rejection_is_definite_and_not_auto_retryable(
    mock_get_client: MagicMock,
) -> None:
    client = MagicMock()
    client.send.side_effect = Exception("NotEnoughBalance", "잔액이 부족합니다")
    mock_get_client.return_value = client

    result = send_one_alimtalk(
        SimpleNamespace(),
        to="01012345678",
        sender="0212345678",
        pf_id="PFID",
        template_id="TEMPLATE",
    )

    client.send.assert_called_once()
    assert result["provider_called"] is True
    assert result["provider_outcome"] == "rejected"
    assert result["definitely_not_accepted"] is True
    assert result["provider_retryable"] is False
    assert _send_failure_disposition(
        result["reason"],
        provider_send_started=result["provider_called"],
        definitely_not_accepted=result["definitely_not_accepted"],
        provider_retryable=result["provider_retryable"],
    ) == "terminal"
