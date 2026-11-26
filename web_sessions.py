"""Process-local Bot login links and WebUI sessions."""
from collections import OrderedDict
from hashlib import sha256
from http.cookies import SimpleCookie, CookieError
import secrets
from threading import Lock
from time import monotonic

TTL = 12 * 3600
LINK_TTL = 120
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
        self.links = OrderedDict()

    @staticmethod
    def digest(token):
        return sha256(token.encode()).digest()

    def issue_link(self, owner):
        with self.lock:
            now = self.clock()
            for key, (uid, expires) in list(self.links.items()):
                if uid == owner or expires <= now:
                    del self.links[key]
            code = secrets.token_urlsafe(32)
            self.links[self.digest(code)] = owner, now + LINK_TTL
            if len(self.links) > 128:
                self.links.popitem(last=False)
            return code

    def redeem(self, code, allowed):
        with self.lock:
            owner, expires = self.links.pop(self.digest(code), (None, 0))
            now = self.clock()
            if expires <= now or owner is None or not allowed(owner):
                return None, None
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.sessions[self.digest(token)] = now + TTL, owner, csrf
            if len(self.sessions) > 128:
                self.sessions.popitem(last=False)
            return token, csrf

    def csrf(self, token, allowed):
        digest = self.digest(token)
        with self.lock:
            expires, owner, csrf = self.sessions.get(digest, (0, None, None))
            if expires <= self.clock() or owner is None or not allowed(owner):
                self.sessions.pop(digest, None)
                return None
            return csrf

    def revoke(self, token):
        with self.lock:
            self.sessions.pop(self.digest(token), None)


SESSIONS = WebSessions()
