"""Offline confidential OIDC protocol and hostile callback regressions."""

import json
import time
import unittest
import urllib.parse
from starlette.datastructures import Headers
from unittest.mock import Mock, patch

from socrat_bot import admin_oidc as q


def headers(value):
    return Headers({"cookie": value})


def metadata():
    return {
        "issuer": q.ISSUER,
        "authorization_endpoint": q.AUTH_ORIGIN + "/application/o/authorize/",
        "token_endpoint": q.AUTH_ORIGIN + "/application/o/token/",
        "jwks_uri": q.ISSUER + "jwks/",
        "code_challenge_methods_supported": ["S256"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.http = Mock(return_value=metadata())
        self.client = q.OIDC("local-fixture", "s" * 48, request=self.http)

    def pending(self):
        target, binding = self.client.begin()
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
        return params, headers(binding.split(";")[0])

    def test_confidential_pkce_nonce_cookie_and_no_open_redirect(self):
        target, binding = self.client.begin()
        p = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
        self.assertEqual(p["redirect_uri"], [q.CALLBACK])
        self.assertEqual(p["scope"], ["openid groups"])
        self.assertEqual(p["code_challenge_method"], ["S256"])
        self.assertNotIn(self.client.client_secret, target + binding)
        self.assertNotIn("code_verifier", p)
        self.assertNotEqual(p["state"], p["nonce"])
        for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=300"):
            self.assertIn(flag, binding)
        self.assertNotIn("Domain=", binding)

    def test_missing_forged_duplicate_state_and_errors_fail_before_exchange(self):
        p, h = self.pending()
        for query, cookie in [
            ("code=x&state=bad", h),
            ("code=x&state=" + p["state"][0], headers("")),
            ("code=x&state=" + p["state"][0] + "&state=other", h),
            ("error=access_denied&state=" + p["state"][0], h),
        ]:
            with self.subTest(query=query), self.assertRaises(q.Rejected):
                self.client.finish(query, cookie)
        self.assertFalse(any(c.args[0] == "POST" for c in self.http.call_args_list))

    def test_exchange_is_one_shot_even_on_network_failure(self):
        p, h = self.pending()
        query = "code=local-code&state=" + p["state"][0]
        self.http.side_effect = q.Unavailable()
        with self.assertRaises(q.Unavailable):
            self.client.finish(query, h)
        with self.assertRaises(q.Rejected):
            self.client.finish(query, h)
        self.assertEqual(self.http.call_count, 2)

    def test_valid_exchange_checks_exact_endpoints_and_keeps_tokens_off_browser(self):
        p, h = self.pending()
        state = p["state"][0]
        expected = self.client.pending[state]
        self.http.side_effect = [{"id_token": "local-token"}, {"keys": []}]
        with patch.object(
            self.client,
            "verify",
            return_value={"sub": "verified", "exp": int(time.time()) + 60},
        ) as verify:
            session = self.client.finish("code=local-code&state=" + state, h)
        verify.assert_called_once_with("local-token", {"keys": []}, expected["nonce"])
        call = self.http.call_args_list[-2]
        self.assertEqual(
            call.args[:2], ("POST", q.AUTH_ORIGIN + "/application/o/token/")
        )
        body = urllib.parse.parse_qs(call.args[2].decode())
        self.assertEqual(body["code_verifier"], [expected["verifier"]])
        self.assertEqual(body["client_secret"], [self.client.client_secret])
        self.assertEqual(body["redirect_uri"], [q.CALLBACK])
        self.assertNotIn("local-token", session)
        self.assertTrue(self.client.authorized(headers(session.split(";")[0])))
        self.client.logout(headers(session.split(";")[0]))
        self.assertFalse(self.client.authorized(headers(session.split(";")[0])))

    def test_metadata_cannot_redirect_credentials_or_choose_issuer(self):
        for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
            data = metadata()
            data[key] = "https://evil.invalid/steal"
            client = q.OIDC("local", "s" * 48, request=Mock(return_value=data))
            with self.subTest(key=key), self.assertRaises(q.Unavailable):
                client.begin()

    def test_expired_and_duplicate_cookies_never_authorize(self):
        identity = "x" * 43
        self.client.sessions[identity] = {"sub": "fixture", "until": time.time() - 1}
        self.assertFalse(
            self.client.authorized(headers(q.SESSION_COOKIE + "=" + identity))
        )
        self.client.sessions[identity] = {"sub": "fixture", "until": time.time() + 60}
        self.assertFalse(
            self.client.authorized(
                headers(
                    q.SESSION_COOKIE
                    + "="
                    + identity
                    + "; "
                    + q.SESSION_COOKIE
                    + "="
                    + identity
                )
            )
        )

    def test_memory_and_outbound_concurrency_are_bounded(self):
        for _ in range(q.MAX_SESSIONS):
            self.client.begin()
        with self.assertRaises(q.Unavailable):
            self.client.begin()
        self.assertEqual(len(self.client.pending), q.MAX_SESSIONS)


@unittest.skipUnless(
    q.jwt is not None, "Pinned crypto is required in the native release image"
)
class SignedTokenTests(unittest.TestCase):
    def test_signed_identity_acceptance_and_negative_protocol_matrix(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        key.update(kid="fixture", use="sig", alg="RS256")
        client = q.OIDC("native-client", "s" * 48)
        now = int(time.time())
        base = {
            "iss": q.ISSUER,
            "aud": "native-client",
            "sub": "00000000-0000-4000-8000-000000000001",
            "iat": now,
            "exp": now + 300,
            "nonce": "native-nonce",
            "groups": ["socrat-bot-admin"],
        }

        def signed(claims):
            return jwt.encode(
                claims, private, algorithm="RS256", headers={"kid": "fixture"}
            )

        self.assertEqual(
            client.verify(signed(base), {"keys": [key]}, "native-nonce")["sub"],
            base["sub"],
        )
        owner = {**base, "groups": ["homelab-owner"]}
        self.assertEqual(
            client.verify(signed(owner), {"keys": [key]}, "native-nonce")["sub"],
            base["sub"],
        )
        for change in (
            {"iss": "https://evil.invalid/"},
            {"aud": "other-client"},
            {"exp": now - 1},
            {"iat": now + 600},
            {"nonce": "other"},
            {"groups": ["homelab-observer"]},
            {"groups": "homelab-owner"},
            {"sub": "same-email@example.invalid"},
            {"azp": "other-client"},
            {"exp": now + 4000},
        ):
            with self.subTest(change=change), self.assertRaises(q.Rejected):
                client.verify(
                    signed({**base, **change}), {"keys": [key]}, "native-nonce"
                )
        for missing in ("iss", "aud", "sub", "exp", "iat", "nonce"):
            claims = {k: v for k, v in base.items() if k != missing}
            with self.subTest(missing=missing), self.assertRaises(q.Rejected):
                client.verify(signed(claims), {"keys": [key]}, "native-nonce")
        with self.assertRaises(q.Rejected):
            client.verify(signed(base), {"keys": [key, key]}, "native-nonce")
        forged = jwt.encode(
            base,
            "forged-local-key-which-is-not-rsa",
            algorithm="HS256",
            headers={"kid": "fixture"},
        )
        with self.assertRaises(q.Rejected):
            client.verify(forged, {"keys": [key]}, "native-nonce")
