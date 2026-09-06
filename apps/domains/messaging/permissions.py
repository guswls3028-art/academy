from apps.core.models import TenantMembership


MESSAGE_SEND_ROLES = ("owner", "admin", "teacher", "staff")


def can_send_messages(request, tenant) -> bool:
    user = request.user
    if not user or not user.is_authenticated or not tenant:
        return False
    if TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        is_active=True,
        role__in=MESSAGE_SEND_ROLES,
    ).exists():
        return True
    return bool(user.is_superuser and getattr(user, "tenant_id", None) == tenant.id)


def can_manage_messaging_settings(request, tenant) -> bool:
    """Only tenant owner/admin may inspect or mutate shared messaging operations."""
    user = request.user
    if not user or not user.is_authenticated or not tenant:
        return False
    if user.is_superuser and getattr(user, "tenant_id", None) == tenant.id:
        return True
    return TenantMembership.objects.filter(
        tenant=tenant,
        user=user,
        is_active=True,
        role__in=("owner", "admin"),
    ).exists()
