"""Native confidential OIDC for the bot grant console; no proxy identities.

Only opaque, host-only session IDs reach the browser. Code/PKCE/client credentials
and tokens stay server-side. Sessions never outlive the verified ID token (5min
in Socrat); restart invalidates them. No email linking or local password login.
"""

import base64
import hashlib
import http.cookies
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import jwt
except ModuleNotFoundError:  # Native release image always includes pinned PyJWT.
    jwt = None

ORIGIN = "https://bot.socrat.alchemmist.xyz"
AUTH_ORIGIN = "https://auth.socrat.alchemmist.xyz"
ISSUER = AUTH_ORIGIN + "/application/o/socrat-bot/"
CALLBACK = ORIGIN + "/oauth/callback"
SESSION_COOKIE = "__Host-bot-session"
STATE_COOKIE = "__Host-bot-state"
GROUPS = frozenset(("homelab-owner", "socrat-bot-admin"))
MAX_SESSIONS = 128


class Rejected(Exception):
    pass


class Unavailable(Exception):
    pass


def cookie(name, value="", age=0):
    return f"{name}={value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={age}"


def cookie_value(headers, name):
    values = headers.getlist("cookie")
    if len(values) != 1 or len(values[0]) > 8192:
        return None
    # Reject duplicate security cookies, rather than letting a parser choose one.
    if sum(p.strip().split("=", 1)[0] == name for p in values[0].split(";")) != 1:
        return None
    try:
        parsed = http.cookies.SimpleCookie(values[0])
        value = parsed[name].value
    except (KeyError, http.cookies.CookieError):
        return None
    return (
        value
        if len(value) == 43
        and value.isascii()
        and all(c.isalnum() or c in "_-" for c in value)
        else None
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OIDC:
    def __init__(self, client_id, client_secret, *, clock=time.time, request=None):
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 256:
            raise ValueError("invalid OIDC client")
        if not isinstance(client_secret, str) or not 32 <= len(client_secret) <= 2048:
            raise ValueError("invalid OIDC credential")
        self.client_id, self.client_secret = client_id, client_secret
        self.clock, self.request = clock, request or self._request
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(4)
        self.pending, self.sessions = {}, {}
        self.metadata = None
        self.metadata_until = 0
        self.http = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def _request(self, method, url, data=None):
        # Endpoints are fixed by metadata() below, never caller/token supplied.
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.http.open(req, timeout=5) as response:
                if response.status != 200:
                    raise Unavailable()
                raw = response.read(65537)
            if len(raw) > 65536:
                raise Unavailable()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise Unavailable()
            return result
        except urllib.error.HTTPError as error:
            error.close()
            # Never retry an ambiguous token exchange or expose IdP responses.
            if method == "POST" and error.code in (400, 401, 403):
                raise Rejected() from None
            raise Unavailable() from None
        except (OSError, ValueError, urllib.error.URLError):
            raise Unavailable() from None

    def discovery(self):
        with self.lock:
            if self.metadata is not None and self.metadata_until > self.clock():
                return dict(self.metadata)
        data = self.request("GET", ISSUER + ".well-known/openid-configuration")
        required = {
            "issuer": ISSUER,
            "authorization_endpoint": AUTH_ORIGIN + "/application/o/authorize/",
            "token_endpoint": AUTH_ORIGIN + "/application/o/token/",
            "jwks_uri": ISSUER + "jwks/",
        }
        if any(data.get(k) != v for k, v in required.items()):
            raise Unavailable()
        if (
            "S256" not in data.get("code_challenge_methods_supported", [])
            or "RS256" not in data.get("id_token_signing_alg_values_supported", [])
            or "client_secret_post"
            not in data.get("token_endpoint_auth_methods_supported", [])
        ):
            raise Unavailable()
        with self.lock:
            self.metadata, self.metadata_until = required, self.clock() + 300
        return dict(required)

    def _prune(self):
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if v["until"] > now}
        self.sessions = {k: v for k, v in self.sessions.items() if v["until"] > now}

    def begin(self):
        if not self.slots.acquire(blocking=False):
            raise Unavailable()
        try:
            metadata = self.discovery()
        finally:
            self.slots.release()
        state, binding, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(4))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        with self.lock:
            self._prune()
            if len(self.pending) >= MAX_SESSIONS:
                raise Unavailable()
            self.pending[state] = {
                "binding": binding,
                "nonce": nonce,
                "verifier": verifier,
                "until": self.clock() + 300,
            }
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": CALLBACK,
                "scope": "openid groups",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return metadata["authorization_endpoint"] + "?" + query, cookie(
            STATE_COOKIE, binding, 300
        )

    def finish(self, query, headers):
        if len(query) > 4096:
            raise Rejected()
        try:
            params = urllib.parse.parse_qs(query, strict_parsing=True, max_num_fields=8)
        except ValueError:
            raise Rejected() from None
        if (
            not {"code", "state"} <= set(params)
            or set(params) - {"code", "state", "iss"}
            or any(len(v) != 1 for v in params.values())
            or ("iss" in params and params["iss"] != [ISSUER])
        ):
            raise Rejected()
        code, state = params["code"][0], params["state"][0]
        if not 1 <= len(code) <= 2048 or len(state) != 43:
            raise Rejected()
        binding = cookie_value(headers, STATE_COOKIE)
        with self.lock:
            self._prune()
            pending = self.pending.get(state)
            if (
                not pending
                or not binding
                or not secrets.compare_digest(binding, pending["binding"])
            ):
                raise Rejected()
            # A code/state gets one attempt even on ambiguous upstream failure.
            del self.pending[state]
        if not self.slots.acquire(blocking=False):
            raise Unavailable()
        try:
            metadata = self.discovery()
            data = urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": CALLBACK,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code_verifier": pending["verifier"],
                }
            ).encode()
            tokens = self.request("POST", metadata["token_endpoint"], data)
            token = tokens.get("id_token")
            if not isinstance(token, str) or len(token) > 16384:
                raise Rejected()
            keys = self.request("GET", metadata["jwks_uri"])
            claims = self.verify(token, keys, pending["nonce"])
        finally:
            self.slots.release()
        identity = secrets.token_urlsafe(32)
        with self.lock:
            self._prune()
            if len(self.sessions) >= MAX_SESSIONS:
                raise Unavailable()
            self.sessions[identity] = {
                "sub": claims["sub"],
                "until": min(claims["exp"], self.clock() + 300),
            }
        return cookie(
            SESSION_COOKIE,
            identity,
            max(0, int(min(claims["exp"] - self.clock(), 300))),
        )

    def verify(self, token, keys, nonce):
        if jwt is None:
            raise Unavailable()
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise Rejected()
            rows = keys.get("keys")
            if not isinstance(rows, list) or len(rows) > 16:
                raise Rejected()
            matches = [
                r
                for r in rows
                if r.get("kid") == header["kid"]
                and r.get("kty") == "RSA"
                and r.get("use", "sig") == "sig"
                and r.get("alg", "RS256") == "RS256"
            ]
            if len(matches) != 1:
                raise Rejected()
            key = jwt.PyJWK.from_dict(matches[0], algorithm="RS256").key
            if key.key_size < 2048:
                raise Rejected()
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=ISSUER,
                audience=self.client_id,
                options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"]},
            )
            if (
                not isinstance(claims["nonce"], str)
                or not secrets.compare_digest(claims["nonce"], nonce)
                or claims.get("azp", self.client_id) != self.client_id
                or (
                    isinstance(claims["aud"], list)
                    and len(claims["aud"]) > 1
                    and claims.get("azp") != self.client_id
                )
                or str(uuid.UUID(claims["sub"])) != claims["sub"]
                or type(claims["exp"]) is not int
                or type(claims["iat"]) is not int
                or not 0 < claims["exp"] - claims["iat"] <= 360
                or not isinstance(claims.get("groups"), list)
                or not all(isinstance(g, str) for g in claims["groups"])
                or not GROUPS.intersection(claims["groups"])
            ):
                raise Rejected()
            return claims
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
            raise Rejected() from None

    def authorized(self, headers):
        identity = cookie_value(headers, SESSION_COOKIE)
        with self.lock:
            self._prune()
            return bool(identity and identity in self.sessions)

    def logout(self, headers):
        identity = cookie_value(headers, SESSION_COOKIE)
        with self.lock:
            self.sessions.pop(identity, None)
        return cookie(SESSION_COOKIE)
