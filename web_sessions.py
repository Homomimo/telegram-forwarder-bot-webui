"""Process-local web sessions: a restart (including a password change) revokes all."""
from collections import OrderedDict
from hashlib import sha256
import hmac
from http.cookies import SimpleCookie, CookieError
import secrets
from threading import Lock
from time import monotonic

TTL = 12 * 3600
COOKIE = 'tfw_session'


def cookie_token(header):
    jar = SimpleCookie()
    try:
        jar.load(header or '')
    except CookieError:
        return ''
    return jar[COOKIE].value if COOKIE in jar else ''


class WebSessions:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.lock = Lock()
        self.sessions = OrderedDict()
        self.failures = OrderedDict()

    @staticmethod
    def digest(token):
        return sha256(token.encode()).digest()

    def login(self, ip, user, password, key):
        with self.lock:
            now = self.clock()
            # Fixed window per peer; never trust a forwarded address.
            until, count = self.failures.get(ip, (0, 0))
            if until <= now:
                until, count = now + 300, 0
            if count >= 8:
                return 429, None, None
            valid = hmac.compare_digest(password.encode(), key.encode())
            if not key or not valid or user != 'admin':
                self.failures[ip] = (until, count + 1)
                self.failures.move_to_end(ip)
                if len(self.failures) > 4096:
                    self.failures.popitem(last=False)
                return 401, None, None
            self.failures.pop(ip, None)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[self.digest(token)] = now + TTL, csrf
            if len(self.sessions) > 128:
                self.sessions.popitem(last=False)
            return 200, token, csrf

    def csrf(self, token):
        digest = self.digest(token)
        with self.lock:
            expires, csrf = self.sessions.get(digest, (0, None))
            if expires <= self.clock():
                self.sessions.pop(digest, None)
                return None
            return csrf

    def revoke(self, token):
        with self.lock:
            self.sessions.pop(self.digest(token), None)
