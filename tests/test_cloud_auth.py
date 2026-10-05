import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import jwt
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa

from niji.cloud_api import create_app
from niji.cloud_auth import InvalidIdentityToken, OIDCVerifier
from niji.durable_run_store import SQLiteRunStore


class StaticJwksClient:
    def __init__(self, key):
        self.key = key

    def get_signing_key_from_jwt(self, _token):
        return SimpleNamespace(key=self.key)


class OIDCVerifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()
        cls.issuer = "https://identity.example.test/"
        cls.audience = "niji-cloud-api"
        cls.verifier = OIDCVerifier(
            cls.issuer, cls.audience, "https://identity.example.test/.well-known/jwks.json",
            jwks_client=StaticJwksClient(cls.public_key),
        )

    def make_token(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": "user-123",
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "test"})

    def test_verified_identity_maps_to_stable_opaque_tenant(self):
        token = self.make_token()
        first = self.verifier.tenant_for_token(token)
        self.assertEqual(first, self.verifier.tenant_for_token(token))
        self.assertRegex(first, r"^oidc_[0-9a-f]{64}$")
        other_user = self.verifier.tenant_for_token(self.make_token(sub="user-456"))
        self.assertNotEqual(first, other_user)

    def test_issuer_audience_expiry_and_subject_are_required(self):
        invalid_claims = (
            {"iss": "https://attacker.test/"},
            {"aud": "another-service"},
            {"exp": int(time.time()) - 60},
            {"sub": ""},
            {"sub": "x" * 256},
            {"iat": None},
        )
        for overrides in invalid_claims:
            with self.subTest(overrides=overrides), self.assertRaises(InvalidIdentityToken):
                self.verifier.tenant_for_token(self.make_token(**overrides))

    def test_only_fixed_rs256_algorithm_is_accepted(self):
        now = int(time.time())
        token = jwt.encode({
            "iss": self.issuer, "aud": self.audience, "sub": "user-123",
            "iat": now, "exp": now + 60,
        }, "attacker-controlled", algorithm="HS256")
        with self.assertRaises(InvalidIdentityToken):
            self.verifier.tenant_for_token(token)

    def test_oversized_or_empty_tokens_are_rejected(self):
        for token in ("", "x" * 16_385):
            with self.subTest(length=len(token)), self.assertRaises(InvalidIdentityToken):
                self.verifier.tenant_for_token(token)

    def test_oidc_configuration_requires_https_and_valid_audience(self):
        for issuer, audience, jwks in (
            ("http://identity.example.test", "api", "https://identity.example.test/keys"),
            ("https://u:p@identity.example.test", "api", "https://identity.example.test/keys"),
            ("https://identity.example.test", "", "https://identity.example.test/keys"),
            ("https://identity.example.test", "api", "http://identity.example.test/keys"),
        ):
            with self.subTest(issuer=issuer, audience=audience), self.assertRaises(ValueError):
                OIDCVerifier(issuer, audience, jwks, jwks_client=StaticJwksClient(self.public_key))


class OIDCAPIIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteRunStore(Path(self.temp.name) / "runs.sqlite3")
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.issuer = "https://identity.example.test/"
        self.audience = "niji-cloud-api"
        self.verifier = OIDCVerifier(
            self.issuer, self.audience, "https://identity.example.test/keys",
            jwks_client=StaticJwksClient(self.private_key.public_key()),
        )
        self.app = create_app(store=self.store, auth_mode="oidc", oidc_verifier=self.verifier)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def headers(self, subject):
        now = int(time.time())
        token = jwt.encode({
            "iss": self.issuer, "aud": self.audience, "sub": subject,
            "iat": now, "exp": now + 300,
        }, self.private_key, algorithm="RS256", headers={"kid": "test"})
        return {"Authorization": f"Bearer {token}"}

    def test_each_verified_user_gets_an_isolated_run_partition(self):
        a_headers = self.headers("alice")
        b_headers = self.headers("bob")
        a = self.client.post("/v1/runs", json={
            "idempotency_key": "same-key", "payload": {"prompt": "private A"},
        }, headers=a_headers)
        b = self.client.post("/v1/runs", json={
            "idempotency_key": "same-key", "payload": {"prompt": "private B"},
        }, headers=b_headers)
        self.assertEqual((a.status_code, b.status_code), (202, 202))
        self.assertNotEqual(a.json()["run_id"], b.json()["run_id"])
        self.assertEqual(len(self.client.get("/v1/runs", headers=a_headers).json()["runs"]), 1)
        self.assertEqual(len(self.client.get("/v1/runs", headers=b_headers).json()["runs"]), 1)
        hidden = self.client.get(f"/v1/runs/{a.json()['run_id']}", headers=b_headers)
        self.assertEqual(hidden.status_code, 404)

    def test_invalid_oidc_token_is_rejected_without_identity_disclosure(self):
        response = self.client.get("/v1/runs", headers={"Authorization": "Bearer not-a-jwt"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Valid bearer token required")


if __name__ == "__main__":
    unittest.main()
