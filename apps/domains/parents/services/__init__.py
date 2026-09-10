# PATH: apps/domains/parents/services.py
"""
학부모 계정 생성/연결 서비스
- 학생 생성 시 학부모 계정 자동 생성
- 학부모 ID = 학부모 전화번호
"""

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.contrib.auth import get_user_model

from apps.core.models import TenantMembership
from apps.support.students.namespace_lock import (
    lock_student_creation_tenant_reference,
)
from ..models import Parent


# 과거 정책: 모든 학부모가 동일 비번 "0000" 사용 → 학부모 전화번호만 알면 자녀 성적/출결 전체 열람.
# 학생 등록에서 초기 비밀번호를 함께 받으면 학생/학부모 계정에 같은 값을 사용한다.
# 독립 복구/legacy ensure에는 학부모 전화번호 마지막 4자리를 안전한 fallback으로 쓴다.
PARENT_DEFAULT_PASSWORD = "0000"  # deprecated — 외부 import 호환용 상수. 신규 코드에서는 절대 쓰지 말 것.


def parent_initial_password(parent_phone: str) -> str:
    """학부모 초기 비번 SSOT — 정규화된 휴대번호의 마지막 4자리."""
    digits = "".join(ch for ch in str(parent_phone or "") if ch.isdigit())
    if len(digits) != 11 or not digits.startswith("010"):
        raise ValueError("학부모 휴대번호를 010 11자리로 입력해 주세요.")
    return digits[-4:]


@dataclass(frozen=True)
class ParentAccountEnsureResult:
    parent: Parent
    user_created: bool
    initial_password: str | None

    @property
    def password_for_notice(self) -> str:
        return self.initial_password or "변경되지 않음"


def ensure_parent_account_for_student(
    *,
    tenant,
    parent_phone: str,
    student_name: str,
    initial_password: str | None = None,
) -> ParentAccountEnsureResult:
    """
    학부모 전화번호로 Parent 조회 또는 생성
    - 없으면 User + Parent + TenantMembership 생성
    - 있으면 기존 Parent 반환 (User 없으면 생성)
    """
    parent_phone = "".join(ch for ch in str(parent_phone or "") if ch.isdigit())
    fallback_pw = parent_initial_password(parent_phone)
    initial_pw = str(initial_password or fallback_pw)
    if len(initial_pw) < 4:
        raise ValueError("학부모 초기 비밀번호는 4자 이상이어야 합니다.")

    User = get_user_model()
    # tenant 내 유일한 학부모 식별: username = p_{tenant_id}_{phone}
    parent_username = f"p_{tenant.id}_{parent_phone}"

    # A concurrent enrollment can discover the same new phone before either
    # transaction commits.  The unique username/parent constraints serialize
    # the collision; retry once after the losing savepoint rolls back.
    for attempt in range(2):
        try:
            with transaction.atomic():
                # Permanent student deletion owns Tenant -> User ordering. An
                # existing parent may share the same User with the deleted
                # student, so take the FK-compatible tenant gate before either
                # Parent or User row locks.
                lock_student_creation_tenant_reference(tenant_id=tenant.id)
                parent = (
                    Parent.objects.select_for_update()
                    .filter(tenant=tenant, phone=parent_phone)
                    .first()
                )
                if parent and parent.user_id:
                    TenantMembership.ensure_active(
                        tenant=tenant,
                        user=parent.user,
                        role="parent",
                    )
                    return ParentAccountEnsureResult(
                        parent=parent,
                        user_created=False,
                        initial_password=None,
                    )

                user = (
                    User.objects.select_for_update()
                    .filter(username=parent_username)
                    .first()
                )
                user_created = user is None
                if user is None:
                    user_name = (parent.name if parent else "") or f"{student_name} 학부모"
                    user = User.objects.create_user(
                        username=parent_username,
                        phone=parent_phone,
                        name=user_name,
                        tenant=tenant,
                    )
                    user.set_password(initial_pw)
                    user.must_change_password = True
                    user.save()
                elif user.tenant_id != tenant.id:
                    raise ValueError("학부모 계정의 테넌트가 일치하지 않습니다.")

                if parent is None:
                    parent = Parent.objects.create(
                        tenant=tenant,
                        user=user,
                        name=f"{student_name} 학부모",
                        phone=parent_phone,
                    )
                elif not parent.user_id:
                    parent.user = user
                    parent.save(update_fields=["user"])

                TenantMembership.ensure_active(
                    tenant=tenant,
                    user=user,
                    role="parent",
                )
                return ParentAccountEnsureResult(
                    parent=parent,
                    user_created=user_created,
                    initial_password=initial_pw if user_created else None,
                )
        except IntegrityError:
            if attempt:
                raise

    raise RuntimeError("학부모 계정을 생성하지 못했습니다.")


def ensure_parent_for_student(
    *,
    tenant,
    parent_phone: str,
    student_name: str,
) -> Parent:
    """Compatibility facade. New notification paths should use the result object."""
    return ensure_parent_account_for_student(
        tenant=tenant,
        parent_phone=parent_phone,
        student_name=student_name,
    ).parent
