"""Real JWT login/password roundtrips; only outbound delivery is stubbed."""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from apps.core.models import OpsAuditLog, PendingPasswordReset, Tenant, TenantDomain, TenantMembership
from apps.core.models.user import user_internal_username
from apps.core.services.password import create_pending_password_reset, force_reset_password
from apps.domains.students.models import Student


pytestmark = pytest.mark.django_db
NOTICE = 'apps.domains.students.services.account_notifications.send_account_recovery_alimtalk'


@pytest.fixture
def account():
    cache.clear()
    tenant = Tenant.objects.create(name='QA Login Roundtrip', code='qa-login-roundtrip')
    TenantDomain.objects.create(tenant=tenant, host='qa-login-roundtrip.test', is_primary=False)
    user = get_user_model().objects.create_user(
        username=user_internal_username(tenant, 'qa-student'),
        password='original-qa-password', tenant=tenant,
        must_change_password=True, token_version=2,
    )
    TenantMembership.objects.create(tenant=tenant, user=user, role='student', is_active=True)
    student = Student.objects.create(
        tenant=tenant, user=user, name='QA Student', ps_number='qa-student',
        omr_code='11112222', phone='01011112222', parent_phone='01033334444',
    )
    client = APIClient(HTTP_HOST='qa-login-roundtrip.test')
    return tenant, user, student, client


def login(client, password='original-qa-password', username='qa-student'):
    client.credentials()
    return client.post('/api/v1/token/', {'username': username, 'password': password}, format='json')


def authenticate(client, response):
    assert response.status_code == 200
    client.credentials(HTTP_AUTHORIZATION='Bearer ' + response.data['access'])


def test_recommended_password_state_allows_real_login_and_profile(account):
    tenant, user, _student, client = account
    response = login(client)
    authenticate(client, response)
    token = AccessToken(response.data['access'])
    assert token['tenant_id'] == tenant.pk
    assert token['token_version'] == 2
    assert token['mcp'] is True
    assert client.get('/api/v1/core/me/').status_code == 200
    assert client.get('/api/v1/student/me/').status_code == 200
    user.refresh_from_db()
    assert user.last_login is None
    assert OpsAuditLog.objects.filter(action='student_activity.login', actor_user=user, target_user=user).count() == 1


@pytest.mark.parametrize('new_password', [
    'replacement-qa-password',
    ' replacement-qa-password',
    'replacement-qa-password ',
    '\treplacement-qa-password\t',
    '\u00a0replacement-qa-password\u3000',
    'replacement qa password',
], ids=['plain', 'leading-space', 'trailing-space', 'tabs', 'unicode-spaces', 'internal-spaces'])
def test_self_change_then_exact_password_login(account, new_password):
    _tenant, user, _student, client = account
    old_tokens = login(client)
    authenticate(client, old_tokens)
    create_pending_password_reset(user, 'pending-qa-password')
    with patch(NOTICE, return_value=True):
        response = client.post('/api/v1/core/change-password/', {
            'old_password': 'original-qa-password', 'new_password': new_password,
        }, format='json')
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.check_password(new_password)
    assert user.must_change_password is False
    assert user.token_version == 3
    assert not PendingPasswordReset.objects.filter(user=user).exists()
    assert client.get('/api/v1/core/me/').status_code == 401
    client.credentials()
    assert client.post('/api/v1/token/refresh/', {'refresh': old_tokens.data['refresh']}, format='json').status_code == 401
    assert login(client).status_code == 400
    new_tokens = login(client, new_password)
    authenticate(client, new_tokens)
    assert AccessToken(new_tokens.data['access'])['mcp'] is False
    assert client.get('/api/v1/core/me/').status_code == 200
    assert client.get('/api/v1/student/me/').status_code == 200
    client.credentials()
    assert client.get('/api/v1/core/me/').status_code == 401
    assert login(client, new_password).status_code == 200


def test_login_does_not_silently_accept_extra_password_whitespace(account):
    _tenant, _user, _student, client = account
    assert login(client, ' original-qa-password ').status_code == 400


def test_notice_failure_preserves_password_pending_reset_and_session(account):
    _tenant, user, _student, client = account
    tokens = login(client)
    authenticate(client, tokens)
    create_pending_password_reset(user, 'pending-qa-password')
    with patch(NOTICE, return_value=False):
        response = client.post('/api/v1/core/change-password/', {
            'old_password': 'original-qa-password', 'new_password': 'replacement-qa-password',
        }, format='json')
    assert response.status_code == 503
    user.refresh_from_db()
    assert user.check_password('original-qa-password')
    assert user.token_version == 2
    assert user.must_change_password is True
    assert PendingPasswordReset.objects.filter(user=user).exists()
    assert client.get('/api/v1/core/me/').status_code == 200
    assert login(client, 'replacement-qa-password').status_code == 400
    assert login(client).status_code == 200


def test_pending_and_staff_reset_preserve_exact_password_and_revoke_tokens(account):
    _tenant, user, _student, client = account
    initial = login(client)
    create_pending_password_reset(user, ' pending-qa-password ')
    pending_tokens = login(client, ' pending-qa-password ')
    authenticate(client, pending_tokens)
    user.refresh_from_db()
    assert user.token_version == 3
    assert user.must_change_password is True
    assert not PendingPasswordReset.objects.filter(user=user).exists()
    assert client.get('/api/v1/core/me/').status_code == 200
    force_reset_password(user, ' staff-reset-qa-password ')
    assert client.get('/api/v1/core/me/').status_code == 401
    assert login(client, ' pending-qa-password ').status_code == 400
    assert login(client, ' staff-reset-qa-password ').status_code == 200
    client.credentials()
    assert client.post('/api/v1/token/refresh/', {'refresh': initial.data['refresh']}, format='json').status_code == 401


def test_wrong_password_inactive_membership_and_cross_tenant_are_denied(account):
    tenant, user, _student, client = account
    assert login(client, 'incorrect-qa-password').status_code == 400
    tokens = login(client)
    authenticate(client, tokens)
    other = Tenant.objects.create(name='QA Other', code='qa-other-roundtrip')
    TenantDomain.objects.create(tenant=other, host='qa-other-roundtrip.test', is_primary=False)
    assert client.get('/api/v1/core/me/', HTTP_HOST='qa-other-roundtrip.test').status_code == 401
    assert client.post('/api/v1/token/', {
        'username': 'qa-student', 'password': 'original-qa-password',
    }, format='json', HTTP_HOST='qa-other-roundtrip.test').status_code == 400
    TenantMembership.objects.filter(tenant=tenant, user=user).update(is_active=False)
    assert login(client).status_code == 400
    client.credentials()
    assert client.post('/api/v1/token/refresh/', {'refresh': tokens.data['refresh']}, format='json').status_code == 401
