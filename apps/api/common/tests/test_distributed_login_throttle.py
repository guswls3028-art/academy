from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api.common.throttles import LoginThrottle, _prune_expired_login_buckets
from apps.core.models import LoginThrottleBucket
from apps.core.services.client_ip import get_client_ip


class RequestStub:
    def __init__(
        self,
        *,
        username="student1",
        tenant_code="academy",
        remote_addr="203.0.113.10",
        forwarded_for="",
        host="api.hakwonplus.com",
    ):
        self.data = {"username": username, "tenant_code": tenant_code}
        self.META = {
            "REMOTE_ADDR": remote_addr,
            "HTTP_X_FORWARDED_FOR": forwarded_for,
        }
        self._host = host

    def get_host(self):
        return self._host


class TrustedClientIpTests(TestCase):
    @override_settings(TRUSTED_PROXY_CIDRS="172.30.0.0/16")
    def test_alb_append_chain_ignores_forged_leading_xff(self):
        request = RequestStub(
            remote_addr="172.30.0.25",
            forwarded_for="198.51.100.99, 203.0.113.44",
        )

        self.assertEqual(get_client_ip(request), "203.0.113.44")

    @override_settings(TRUSTED_PROXY_CIDRS="172.30.0.0/16")
    def test_untrusted_direct_peer_cannot_supply_xff(self):
        request = RequestStub(
            remote_addr="203.0.113.44",
            forwarded_for="198.51.100.99",
        )

        self.assertEqual(get_client_ip(request), "203.0.113.44")


@override_settings(TRUSTED_PROXY_CIDRS="")
class DistributedLoginThrottleTests(TestCase):
    def test_account_limit_is_shared_across_source_ips(self):
        for index in range(LoginThrottle.ACCOUNT_LIMIT):
            request = RequestStub(remote_addr=f"203.0.113.{index + 1}")
            self.assertTrue(LoginThrottle().allow_request(request, None))

        denied = LoginThrottle()
        self.assertFalse(
            denied.allow_request(RequestStub(remote_addr="198.51.100.55"), None)
        )
        self.assertGreater(denied.wait(), 0)

    def test_bucket_keys_never_store_plain_identity_or_ip(self):
        request = RequestStub(
            username="010-1234-5678",
            tenant_code="secret-academy",
            remote_addr="203.0.113.77",
        )
        self.assertTrue(LoginThrottle().allow_request(request, None))

        keys = list(LoginThrottleBucket.objects.values_list("bucket_key", flat=True))
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(len(key) == 64 for key in keys))
        self.assertTrue(all("secret-academy" not in key for key in keys))
        self.assertTrue(all("203.0.113.77" not in key for key in keys))

    def test_expired_window_resets_atomically(self):
        request = RequestStub()
        self.assertTrue(LoginThrottle().allow_request(request, None))
        LoginThrottleBucket.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1),
            request_count=999,
        )

        self.assertTrue(LoginThrottle().allow_request(request, None))
        self.assertTrue(
            all(
                count == 1
                for count in LoginThrottleBucket.objects.values_list(
                    "request_count", flat=True
                )
            )
        )

    def test_storage_prune_deletes_only_long_expired_buckets(self):
        now = timezone.now()
        LoginThrottleBucket.objects.create(
            bucket_key="a" * 64,
            scope="ip",
            request_count=1,
            window_started_at=now - timedelta(days=3),
            expires_at=now - timedelta(days=2),
        )
        LoginThrottleBucket.objects.create(
            bucket_key="b" * 64,
            scope="ip",
            request_count=1,
            window_started_at=now,
            expires_at=now + timedelta(minutes=1),
        )

        _prune_expired_login_buckets()

        self.assertFalse(LoginThrottleBucket.objects.filter(bucket_key="a" * 64).exists())
        self.assertTrue(LoginThrottleBucket.objects.filter(bucket_key="b" * 64).exists())


@override_settings(ALLOWED_HOSTS=["testserver"], TRUSTED_PROXY_CIDRS="")
class LoginTokenMethodBoundaryTests(TestCase):
    def test_get_is_rejected_before_login_throttle_writes(self):
        response = APIClient().get("/api/v1/token/")

        self.assertEqual(response.status_code, 405)
        self.assertFalse(LoginThrottleBucket.objects.exists())

    def test_post_still_consumes_distributed_login_throttle_buckets(self):
        response = APIClient().post(
            "/api/v1/token/",
            {
                "username": "missing-user",
                "password": "wrong-password",
                "tenant_code": "missing-tenant",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            set(LoginThrottleBucket.objects.values_list("scope", flat=True)),
            {"ip", "account"},
        )
