# PATH: apps/domains/parents/services.py
"""학부모 계정을 명시적 비밀번호로 만들거나 기존 계정에 연결한다."""

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import identify_hasher
from django.db import IntegrityError, transaction

from academy.adapters.db.django import repositories_core as core_repo
from apps.core.models import TenantMembership
from ..models import Parent


def normalize_parent_phone(parent_phone: str) -> str:
    digits = "".join(ch for ch in str(parent_phone or "") if ch.isdigit())
    if len(digits) != 11 or not digits.startswith("010"):
        raise ValueError("학부모 휴대번호를 010 11자리로 입력해 주세요.")
    return digits


def _assert_parent_login_identity_available(
    *,
    tenant,
    phone: str,
    user_id: int | None = None,
) -> None:
    conflicts = [
        user
        for user in core_repo.user_list_by_tenant_login_identifier(tenant, phone)
        if user.id != user_id
    ]
    if conflicts:
        raise ValueError(
            "학부모 전화번호가 다른 계정의 로그인 아이디로 사용 중입니다."
        )


@dataclass(frozen=True)
class ParentAccountEnsureResult:
    parent: Parent
    credentials_initialized: bool
    password_notice: str | None

    @property
    def password_for_notice(self) -> str:
        return self.password_notice or "변경되지 않음"


def ensure_parent_account_for_student(
    *,
    tenant,
    parent_phone: str,
    student_name: str,
    initial_password: str | None = None,
    initial_password_hash: str | None = None,
    initial_password_notice: str | None = None,
) -> ParentAccountEnsureResult:
    """
    학부모 전화번호로 기존 계정을 찾거나 명시적 자격 증명으로 새 계정을 만든다.

    사용 가능한 기존 계정의 비밀번호는 절대 변경하지 않는다. 새 User를 만들거나
    비밀번호가 없는 미완성 계정을 복구할 때만 호출자가 입력한 초기 비밀번호 또는
    가입 신청의 검증된 비밀번호 hash를 사용한다.
    """
    parent_phone = normalize_parent_phone(parent_phone)
    initial_pw = str(initial_password or "")
    password_hash = str(initial_password_hash or "")
    password_notice = str(initial_password_notice or initial_pw)
    if initial_pw and password_hash:
        raise ValueError("학부모 초기 비밀번호와 비밀번호 해시는 함께 입력할 수 없습니다.")
    if initial_pw and len(initial_pw) < 4:
        raise ValueError("학부모 초기 비밀번호는 4자 이상이어야 합니다.")
    if password_hash:
        identify_hasher(password_hash)
        if not password_notice:
            raise ValueError("학부모 계정 안내값이 필요합니다.")

    User = get_user_model()
    # tenant 내 유일한 학부모 식별: username = p_{tenant_id}_{phone}
    parent_username = f"p_{tenant.id}_{parent_phone}"

    # A concurrent enrollment can discover the same new phone before either
    # transaction commits.  The unique username/parent constraints serialize
    # the collision; retry once after the losing savepoint rolls back.
    for attempt in range(2):
        try:
            with transaction.atomic():
                parent = (
                    Parent.objects.select_for_update()
                    .filter(tenant=tenant, phone=parent_phone)
                    .first()
                )
                if parent and parent.user_id:
                    _assert_parent_login_identity_available(
                        tenant=tenant,
                        phone=parent_phone,
                        user_id=parent.user_id,
                    )
                    if not parent.user.is_active:
                        raise ValueError("비활성화된 학부모 계정입니다. 계정 복구 후 다시 시도해 주세요.")
                    credentials_initialized = False
                    if not parent.user.has_usable_password():
                        if not initial_pw and not password_hash:
                            raise ValueError(
                                "학부모 계정을 사용하려면 초기 비밀번호를 입력해 주세요."
                            )
                        if password_hash:
                            parent.user.password = password_hash
                        else:
                            parent.user.set_password(initial_pw)
                        parent.user.must_change_password = True
                        parent.user.save(update_fields=["password", "must_change_password"])
                        credentials_initialized = True
                    TenantMembership.ensure_active(
                        tenant=tenant,
                        user=parent.user,
                        role="parent",
                    )
                    return ParentAccountEnsureResult(
                        parent=parent,
                        credentials_initialized=credentials_initialized,
                        password_notice=password_notice if credentials_initialized else None,
                    )

                user = (
                    User.objects.select_for_update()
                    .filter(username=parent_username)
                    .first()
                )
                if user is None:
                    if not initial_pw and not password_hash:
                        raise ValueError(
                            "새 학부모 계정을 만들려면 초기 비밀번호를 입력해 주세요."
                        )
                    _assert_parent_login_identity_available(
                        tenant=tenant,
                        phone=parent_phone,
                    )
                    user_name = (parent.name if parent else "") or f"{student_name} 학부모"
                    user = User.objects.create_user(
                        username=parent_username,
                        phone=parent_phone,
                        name=user_name,
                        tenant=tenant,
                    )
                elif user.tenant_id != tenant.id:
                    raise ValueError("학부모 계정의 테넌트가 일치하지 않습니다.")
                else:
                    _assert_parent_login_identity_available(
                        tenant=tenant,
                        phone=parent_phone,
                        user_id=user.id,
                    )

                if not user.is_active:
                    raise ValueError("비활성화된 학부모 계정입니다. 계정 복구 후 다시 시도해 주세요.")
                credentials_initialized = False
                if not user.has_usable_password():
                    if not initial_pw and not password_hash:
                        raise ValueError(
                            "학부모 계정을 사용하려면 초기 비밀번호를 입력해 주세요."
                        )
                    if password_hash:
                        user.password = password_hash
                    else:
                        user.set_password(initial_pw)
                    user.must_change_password = True
                    user.save(update_fields=["password", "must_change_password"])
                    credentials_initialized = True

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
                    credentials_initialized=credentials_initialized,
                    password_notice=password_notice if credentials_initialized else None,
                )
        except IntegrityError:
            if attempt:
                raise

    raise RuntimeError("학부모 계정을 생성하지 못했습니다.")


def find_parent_account(*, tenant, parent_phone: str) -> Parent | None:
    """Return an existing tenant-scoped parent account without creating one."""
    phone = normalize_parent_phone(parent_phone)
    return (
        Parent.objects.filter(tenant=tenant, phone=phone)
        .select_related("user")
        .first()
    )


def parent_account_needs_password(*, tenant, parent_phone: str) -> bool:
    """Return whether this identity lacks usable credentials and needs an explicit password."""
    phone = normalize_parent_phone(parent_phone)
    parent = find_parent_account(tenant=tenant, parent_phone=phone)
    if parent and parent.user_id:
        return not parent.user.has_usable_password()
    username = f"p_{tenant.id}_{phone}"
    user = get_user_model().objects.filter(tenant=tenant, username=username).first()
    return user is None or not user.has_usable_password()
