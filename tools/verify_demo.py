"""Offline acceptance check. Only writes fixture jobs to a running demo instance."""

from http import cookiejar
import json
import os
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import build_opener, HTTPCookieProcessor, Request


def verify(base, password):
    client = build_opener(HTTPCookieProcessor(cookiejar.CookieJar()))
    csrf = ""

    def api(op, data=None, token=None):
        headers = {
            "Content-Type": "application/json",
            "X-Relay-Request": "1",
            "X-CSRF-Token": csrf if token is None else token,
        }
        req = Request(
            base + "/api/" + op,
            None if data is None else json.dumps(data).encode(),
            headers=headers,
        )
        with client.open(req, timeout=15) as response:
            return json.load(response)

    def denied(status, fn):
        try:
            fn()
        except HTTPError as exc:
            with exc:
                assert exc.code == status, (status, exc.code)
        else:
            raise AssertionError("request unexpectedly accepted")

    denied(401, lambda: api("state"))
    csrf = api("auth/login", {"username": "admin", "password": password})["csrf"]
    before = api("state")
    if before["mode"] != "demo" or before["config"]["target"] != "1234567890":
        raise RuntimeError("requires an isolated demo with its initial target")
    expected = (Path(__file__).resolve().parents[1] / "VERSION").read_text("ascii").strip()
    assert before["version"] == expected, (before["version"], expected)
    for _ in range(40):
        if before["queue"]["running"]:
            break
        time.sleep(0.25)
        before = api("state")
    else:
        raise RuntimeError("demo queue did not become ready")
    denied(403, lambda: api("demo-bot", {}, "wrong"))
    denied(400, lambda: api("login/start", {"provider": "tdl"}))

    def payload(first, last=None):
        return dict(
            key=uuid.uuid4().hex,
            src=f"https://t.me/c/9876543210/{first}",
            target="1234567890",
            kind="range" if last else "link",
            end=last,
        )

    p = payload(40, 46)
    denied(400, lambda: api("submit", {**p, "target": "9999"}))
    jid = api("submit", p)["id"]
    assert api("submit", p)["id"] == jid
    api("demo-bot", {})
    cut = api("submit", payload(42, 46))["id"]
    lost = api("submit", payload(47))["id"]
    for _ in range(120):
        state = api("state")
        history = {row["job_id"]: row for row in state["history"] if row.get("job_id")}
        if lost in history and any(item["id"] == lost for item in state["reviews"]):
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("demo worker did not complete in time")
    for job, origin in [(jid, "web"), (jid + 1, "bot")]:
        row = history[job]
        assert (
            row["success"] and row["origin"] == origin and "TDL clone" in row["route"]
        ), row
        assert (
            row["album_groups"] == [[41, 42, 43], [45, 46]] and len(row["members"]) == 7
        )
        assert all(target > 0 for _, target in row["members"])
    assert not history[cut]["success"] and "截断" in history[cut]["error"]
    assert history[lost]["unconfirmed"] and history[lost]["members"] == [[47, 0]]
    print(
        "PASS: auth, CSRF, QR isolation, target snapshot, idempotency, both entries, album grouping, restricted fallback and uncertain receipts"
    )
    return lost


if __name__ == "__main__":
    data = Path(os.getenv("RELAY_DATA", "/data")) / "demo"
    secret = os.getenv("RELAY_PASSWORD") or json.loads(
        (data / "web-password.json").read_text("utf-8")
    )
    verify("http://127.0.0.1:" + os.getenv("PORT", "8765"), secret)
