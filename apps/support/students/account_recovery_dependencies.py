"""Cross-domain dependencies for student account recovery."""

from __future__ import annotations

from typing import Any

def find_parent_recovery_account(
    *,
    tenant: Any,
    parent_phone: str,
) -> Any | None:
    from apps.domains.parents.services import find_parent_account

    return find_parent_account(
        tenant=tenant,
        parent_phone=parent_phone,
    )


def account_recovery_delivery_disabled(source_tenant_id: int) -> bool:
    from apps.domains.messaging.policy import is_messaging_disabled

    # 공용 owner 학원의 고객용 off 설정은 다른 학원의 계정 복구를 막지 않는다.
    # 실제 owner 채널 긴급 hold는 send_alimtalk_via_owner 경계에서 별도 적용된다.
    return is_messaging_disabled(source_tenant_id)


def send_account_recovery_alimtalk(**kwargs: Any) -> bool:
    from apps.domains.messaging.policy import send_alimtalk_via_owner

    return send_alimtalk_via_owner(**kwargs)
