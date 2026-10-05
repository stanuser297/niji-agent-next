"""Verified OIDC bearer-token authentication for hosted cloud APIs.

The identity provider is fixed by trusted server configuration. User-supplied
JWT headers and claims never select an issuer, key URL, or signing algorithm.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit

_MAX_BEARER_TOKEN_CHARS = 16_384


class InvalidIdentityToken(ValueError):
    """Raised when an OIDC token cannot be trusted or mapped to a user."""


class OIDCVerifier:
    """Verify RS256 access tokens against a configured OIDC issuer and JWKS."""

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str,
        *,
        jwks_client=None,
    ):
        self.issuer = self._https_url(issuer, "OIDC issuer")
        self.audience = audience.strip() if isinstance(audience, str) else ""
        if not self.audience or len(self.audience) > 512:
            raise ValueError("NIJI_CLOUD_OIDC_AUDIENCE must be 1-512 characters")
        self.jwks_url = self._https_url(jwks_url, "OIDC JWKS URL")
        if jwks_client is None:
            try:
                from jwt import PyJWKClient
            except ImportError as exc:  # pragma: no cover - optional extra
                raise RuntimeError("Install Niji's cloud extra with OIDC support") from exc
            jwks_client = PyJWKClient(
                self.jwks_url, cache_jwk_set=True, lifespan=300, timeout=5,
            )
        self._jwks_client = jwks_client

    @staticmethod
    def _https_url(value: str, label: str) -> str:
        if not isinstance(value, str) or len(value) > 2_048:
            raise ValueError(f"{label} must be an HTTPS URL")
        value = value.strip()
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.fragment):
            raise ValueError(f"{label} must be an HTTPS URL without credentials or fragment")
        return value

    def tenant_for_token(self, token: str) -> str:
        """Return an opaque stable storage partition derived from verified identity."""
        if not isinstance(token, str) or not token or len(token) > _MAX_BEARER_TOKEN_CHARS:
            raise InvalidIdentityToken("Invalid bearer token")
        try:
            import jwt
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
                leeway=30,
            )
        except Exception as exc:
            # Do not disclose whether an issuer, key, signature, or claim failed.
            raise InvalidIdentityToken("Invalid bearer token") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise InvalidIdentityToken("Invalid bearer token")
        material = (self.issuer + "\0" + subject).encode("utf-8")
        return "oidc_" + hashlib.sha256(material).hexdigest()
