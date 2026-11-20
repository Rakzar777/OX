import hashlib
import hmac
import re
import secrets
import time


SESSION_COOKIE = "__Host-ox_volunteer"
SESSION_SECONDS = 24 * 60 * 60
PASSWORD_ITERATIONS = 600_000
PASSWORD_PATTERN = re.compile(r"pbkdf2_sha256:600000:([0-9a-f]{32}):([0-9a-f]{64})")


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_ITERATIONS
    ).hex()
    return "pbkdf2_sha256:600000:" + salt + ":" + digest


class VolunteerAuth:
    def __init__(self, settings):
        self.settings = settings

    @property
    def configured(self):
        return bool(
            self.settings.volunteer_username
            and PASSWORD_PATTERN.fullmatch(self.settings.volunteer_password_hash)
            and len(self.settings.volunteer_session_secret) >= 32
        )

    def verify_credentials(self, username, password):
        if not self.configured or len(username) > 128 or len(password) > 256:
            return False
        salt, expected = PASSWORD_PATTERN.fullmatch(self.settings.volunteer_password_hash).groups()
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), PASSWORD_ITERATIONS
        ).hex()
        user_matches = hmac.compare_digest(
            username.encode("utf-8"), self.settings.volunteer_username.encode("utf-8")
        )
        return hmac.compare_digest(actual, expected) and user_matches

    def signature(self, payload):
        # Changing the shared credentials also invalidates all previously issued sessions.
        binding = self.settings.volunteer_username + ":" + self.settings.volunteer_password_hash
        key = hmac.new(
            self.settings.volunteer_session_secret.encode("utf-8"),
            binding.encode("utf-8"), hashlib.sha256,
        ).digest()
        return hmac.new(key, payload.encode("ascii"), hashlib.sha256).hexdigest()

    def issue_session(self):
        payload = f"{int(time.time()) + SESSION_SECONDS}.{secrets.token_urlsafe(24)}"
        return payload + "." + self.signature(payload)

    def authenticated(self, request):
        if not self.configured:
            return False
        value = request.cookies.get(SESSION_COOKIE, "")
        if not re.fullmatch(r"[0-9]{1,12}\.[A-Za-z0-9_-]{32}\.[0-9a-f]{64}", value):
            return False
        payload, signature = value.rsplit(".", 1)
        expiry = int(payload.split(".", 1)[0])
        return expiry > time.time() and hmac.compare_digest(signature, self.signature(payload))
