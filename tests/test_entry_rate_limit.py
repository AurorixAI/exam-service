"""#33: the public exam entry must not be a roster oracle.

Anyone holding an exam code can ask «is TE-031 in the group?» and gets a name back on a match.  The answer stays
(the page shows the confirmed student) but walking the ID space is made expensive: strict budget for FAILED lookups
per (exam code, client address), a per-code circuit breaker, a generic refusal text, 429 + Retry-After.  A whole
class behind one school NAT must still get in.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("APP_ENV", "testing")

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from src.api import router
from src.core import abuse_limit
from src.core.abuse_limit import (
    ATTEMPTS_PER_MINUTE,
    CODE_FAILS_SOFT,
    FAILS_PER_HOUR,
    FAILS_PER_MINUTE,
    SUSPECT_IP_FAILS,
    client_ip,
    code_key,
    counters,
    limit_bucket,
)
from src.models.exam_models import GeneratedExam
from tests.conftest import make_exam_in_db, make_task

CODE = "EXRATE0001"
OTHER_CODE = "EXRATE0002"
GENERIC = "Ученик не найден в этой группе — проверьте имя или ID"
TOO_MANY = "Слишком много попыток. Подождите минуту и попробуйте снова."
TASKS = [make_task(f"TASK_{i:03d}", answer=str(i)) for i in range(1, 3)]


def _roster(n=40):
    return {
        f"TE-{i:03d}": {
            "id": f"aaaaaaaa-0000-4000-8000-{i:012d}",
            "full_name": f"Фамилия{i} Имя{i}",
            "display_id": f"TE-{i:03d}",
            "class_level": 8,
        }
        for i in range(1, n + 1)
    }


ROSTER = _roster()


@pytest.fixture
def roster(monkeypatch):
    """auth-service stand-in at the HTTP level (the real _canonical_student_for_exam runs): exact ID or a refusal
    whose reason is as specific as the real one - the point is that it must not reach the student."""

    class _Reply:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _AuthClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            params = params or {}
            key = (params.get("student_id") or params.get("q") or "").strip().upper()
            if key in ROSTER:
                return _Reply({"enrolled": True, "group_name": "8А", "student": dict(ROSTER[key])})
            return _Reply({"enrolled": False, "group_name": "8А", "message": f"Ученик с ID «{key}» не найден (подробная причина auth-service)"})

    monkeypatch.setattr(router, "_canonical_student_for_exam", _REAL_CANONICAL)
    monkeypatch.setattr(router.httpx, "AsyncClient", _AuthClient)


@pytest.fixture
def exam(db):
    e = make_exam_in_db(db, status="active", tasks=TASKS)
    e.access_code = CODE
    db.commit()
    return e


def check(client, student_id, *, ip="203.0.113.7", code=CODE, **extra):
    return client.post(
        f"/api/v1/exams/by-code/{code}/check-student",
        json={"student_id": student_id, "identifier": student_id, **extra},
        headers={"X-Real-IP": ip},
    )


def start(client, student_id, *, ip="203.0.113.7", code=CODE):
    return client.post(
        f"/api/v1/exams/by-code/{code}/start",
        json={"student_name": "x", "student_id": student_id, "display_id": student_id},
        headers={"X-Real-IP": ip},
    )


# ── the client address: only the header our proxy sets ───────────────────────────────────────────────────

def _req(headers=None, peer="10.0.0.9"):
    return SimpleNamespace(headers={k.lower(): v for k, v in (headers or {}).items()}, client=SimpleNamespace(host=peer))


class TestClientAddress:
    def test_real_ip_wins(self):
        assert client_ip(_req({"X-Real-IP": "198.51.100.4"})) == "198.51.100.4"

    def test_forwarded_for_is_never_trusted(self):
        # nginx sets only X-Real-IP on the exam locations: X-Forwarded-For is whatever the client typed.
        assert client_ip(_req({"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})) == "10.0.0.9"
        assert client_ip(_req({"X-Real-IP": "198.51.100.4", "X-Forwarded-For": "1.2.3.4"})) == "198.51.100.4"

    def test_garbage_real_ip_falls_back_to_the_peer(self):
        assert client_ip(_req({"X-Real-IP": "not-an-ip"})) == "10.0.0.9"
        assert client_ip(_req({"X-Real-IP": "' OR 1=1 --"})) == "10.0.0.9"

    def test_ipv6_counts_by_its_64(self):
        a = limit_bucket("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
        b = limit_bucket("2001:db8:1:2:1111:2222:3333:4444")
        c = limit_bucket("2001:db8:1:3::1")
        assert a == b == "2001:db8:1:2::/64" and c != a

    def test_ipv4_mapped_ipv6_is_the_ipv4_client(self):
        assert limit_bucket("::ffff:203.0.113.7") == "203.0.113.7"
        assert limit_bucket("203.0.113.7") == "203.0.113.7"

    def test_the_code_key_is_fixed_size_and_case_blind(self):
        assert code_key("exabc12345") == code_key(" EXABC12345 ")
        assert code_key("EXABC12345") != code_key("EXABC12346")
        assert len(code_key("x" * 100_000)) == len(code_key("a"))


# ── counters ──────────────────────────────────────────────────────────────────────────────────────────────

class _Clock:
    """The limiter's own clock (not time.monotonic: the event loop of the test client uses that one)."""

    def __init__(self, monkeypatch):
        self.now = 1000.0
        monkeypatch.setattr(abuse_limit, "_now", lambda: self.now)


class TestCounters:
    def test_a_window_counts_then_expires(self, monkeypatch):
        clock = _Clock(monkeypatch)
        assert counters.hit("k", 60) == (1, 60)
        clock.now += 20
        assert counters.hit("k", 60) == (2, 40)
        assert counters.peek("k") == (2, 40)
        clock.now += 41
        assert counters.peek("k") == (0, 0)
        assert counters.hit("k", 60) == (1, 60)

    def test_peek_does_not_count(self):
        assert counters.peek("fresh") == (0, 0)
        assert counters.peek("fresh") == (0, 0)
        assert counters.hit("fresh", 60)[0] == 1

    def test_memory_does_not_grow_without_bound(self, monkeypatch):
        monkeypatch.setattr(abuse_limit, "_MEMORY_SOFT_LIMIT", 50)
        monkeypatch.setattr(abuse_limit, "_MEMORY_HARD_LIMIT", 100)
        for i in range(1000):
            counters.hit(f"flood:{i}", 3600)
        assert len(counters._memory) <= 100


class _FakeRedis:
    """Just enough of redis-py for the limiter: eval (the INCR+EXPIRE script), get/ttl through a pipeline."""

    def __init__(self):
        self.values, self.ttls, self.evals = {}, {}, 0

    def eval(self, script, numkeys, key, window):
        self.evals += 1
        self.values[key] = self.values.get(key, 0) + 1
        if self.values[key] == 1:
            self.ttls[key] = int(window)
        return [self.values[key], self.ttls[key]]

    def pipeline(self):
        outer = self

        class _Pipe:
            def __init__(self):
                self.calls = []

            def get(self, key):
                self.calls.append(("get", key))

            def ttl(self, key):
                self.calls.append(("ttl", key))

            def execute(self):
                return [
                    (str(outer.values[k]) if k in outer.values else None) if op == "get" else outer.ttls.get(k, -2)
                    for op, k in self.calls
                ]

        return _Pipe()


class _BrokenRedis:
    def eval(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    def pipeline(self):
        raise ConnectionError("redis is down")


class TestRedisBackend:
    def test_counts_live_in_redis_when_it_is_there(self):
        fake = _FakeRedis()
        counters.use_redis(fake)
        assert counters.hit("k", 60) == (1, 60)
        assert counters.hit("k", 60) == (2, 60)
        assert counters.peek("k") == (2, 60)
        assert counters.peek("absent") == (0, 0)
        assert "k" not in counters._memory

    def test_redis_down_falls_back_to_memory_with_one_warning(self, caplog):
        counters.use_redis(_BrokenRedis())
        with caplog.at_level("WARNING", logger="exam_service.abuse_limit"):
            for _ in range(5):
                counters.hit("k", 60)
        assert counters.peek("k")[0] == 5  # still counted, in process memory
        warnings = [r for r in caplog.records if "abuse_limit_redis_unavailable" in r.getMessage()]
        assert len(warnings) == 1

    def test_redis_is_not_retried_on_every_request_while_it_is_down(self, monkeypatch):
        clock = _Clock(monkeypatch)
        broken = _BrokenRedis()
        calls = []
        broken.eval = lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(ConnectionError("down"))
        counters.use_redis(broken)
        for _ in range(10):
            counters.hit("k", 60)
        assert len(calls) == 1
        clock.now += abuse_limit._REDIS_RETRY_AFTER + 1
        counters.hit("k", 60)
        assert len(calls) == 2

    @pytest.mark.skipif(not os.getenv("RATE_LIMIT_REDIS_URL"), reason="set RATE_LIMIT_REDIS_URL to run against a real Redis")
    def test_real_redis_script(self):
        import redis

        client = redis.from_url(os.environ["RATE_LIMIT_REDIS_URL"], decode_responses=True)
        client.delete("rlt:k")
        counters.use_redis(client)
        try:
            assert counters.hit("rlt:k", 60)[0] == 1
            count, ttl = counters.hit("rlt:k", 60)
            assert count == 2 and 0 < ttl <= 60
            assert counters.peek("rlt:k")[0] == 2
            assert counters.peek("rlt:none") == (0, 0)
        finally:
            client.delete("rlt:k")


# ── the endpoints ─────────────────────────────────────────────────────────────────────────────────────────

class TestLegitimateClassIsNotLockedOut:
    def test_a_class_of_thirty_behind_one_nat_joins_in_one_minute(self, client, db, exam, roster):
        """30 students x (check-student + start) from ONE address: 60 attempts, all pass."""
        for i in range(1, 31):
            sid = f"TE-{i:03d}"
            checked = check(client, sid)
            assert checked.status_code == 200, (sid, checked.text)
            assert checked.json()["enrolled"] is True
            started = start(client, sid)
            assert started.status_code == 200, (sid, started.text)

    def test_a_few_typos_in_the_class_do_not_hurt(self, client, db, exam, roster):
        """A class has a handful of failed lookups (typos), far below the strict budget of its address."""
        for i in range(1, 6):
            assert check(client, f"TE-9{i:02d}").json()["enrolled"] is False  # 5 typos
        for i in range(1, 31):
            sid = f"TE-{i:03d}"
            assert check(client, sid).status_code == 200
            assert start(client, sid).status_code == 200

    def test_two_classes_with_different_codes_behind_one_nat_have_separate_budgets(self, client, db, exam, roster):
        second = GeneratedExam(
            id="EXAM_TEST02", title="Второй", source_mode="topics", variants=exam.variants, settings=exam.settings,
            skills_covered=["SK_001"], toc_ids_used=[], status="active", total_time_minutes=45, variants_count=1,
            tasks_per_variant=2, org_id=exam.org_id, created_by=exam.created_by, group_id=exam.group_id,
            group_name="8Б", class_level=8, scheduled_lesson_id="LSN_TEST02",
            scheduled_lesson_date=exam.scheduled_lesson_date, access_code=OTHER_CODE,
        )
        db.add(second)
        db.commit()
        for i in range(FAILS_PER_MINUTE.count):
            assert check(client, f"TE-9{i:02d}").status_code == 200
        assert check(client, "TE-001").status_code == 429  # the first class's budget at this address is spent...
        other = check(client, "TE-001", code=OTHER_CODE)  # ...the second class (same NAT) is untouched
        assert other.status_code == 200 and other.json()["enrolled"] is True


class TestFailedLookupsAreBudgeted:
    def test_the_sixteenth_failure_in_a_minute_is_refused_with_retry_after(self, client, db, exam, roster):
        for i in range(FAILS_PER_MINUTE.count):
            body = check(client, f"TE-9{i:02d}")
            assert body.status_code == 200 and body.json()["enrolled"] is False
        refused = check(client, "TE-001")  # even the right ID: the budget of this address is spent
        assert refused.status_code == 429
        assert refused.json()["detail"] == TOO_MANY
        assert 1 <= int(refused.headers["Retry-After"]) <= 60

    def test_another_address_is_not_affected(self, client, db, exam, roster):
        for i in range(FAILS_PER_MINUTE.count):
            check(client, f"TE-9{i:02d}")
        assert check(client, "TE-001").status_code == 429
        assert check(client, "TE-001", ip="198.51.100.20").status_code == 200

    def test_the_budget_comes_back_after_the_window(self, client, db, exam, roster, monkeypatch):
        clock = _Clock(monkeypatch)
        for i in range(FAILS_PER_MINUTE.count):
            check(client, f"TE-9{i:02d}")
        assert check(client, "TE-001").status_code == 429
        clock.now += 61
        assert check(client, "TE-001").status_code == 200

    def test_the_hour_budget_holds_when_the_minute_budget_is_respected(self, client, db, exam, roster, monkeypatch):
        clock = _Clock(monkeypatch)
        spent = 0
        while spent < FAILS_PER_HOUR.count:
            for _ in range(FAILS_PER_MINUTE.count):
                if spent == FAILS_PER_HOUR.count:
                    break
                assert check(client, f"TE-9{spent % 99:02d}").status_code == 200
                spent += 1
            clock.now += 61  # a fresh minute, the hour goes on
        refused = check(client, "TE-001")
        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) > 60
        assert "мин" in refused.json()["detail"] and refused.json()["detail"] != TOO_MANY

    def test_a_walk_over_the_ids_is_stopped_after_the_budget(self, client, db, exam, roster):
        """The attacker's currency is failed guesses: of 200 IDs only a handful are read before the 429."""
        read, refused = 0, False
        for n in range(1, 201):
            response = check(client, f"TE-{n:03d}" if n <= 40 and n % 3 == 0 else f"TE-8{n:03d}")
            if response.status_code == 429:
                refused = True
                break
            read += 1 if response.json()["enrolled"] else 0
        assert refused and read <= 13

    def test_start_failures_spend_the_same_budget(self, client, db, exam, roster):
        for i in range(FAILS_PER_MINUTE.count):
            assert start(client, f"TE-9{i:02d}").status_code == 403
        assert start(client, "TE-001").status_code == 429
        assert check(client, "TE-001").status_code == 429

    def test_a_success_does_not_spend_the_failure_budget(self, client, db, exam, roster):
        for _ in range(3):
            for i in range(1, 11):
                assert check(client, f"TE-{i:03d}").json()["enrolled"] is True
        assert check(client, "TE-9").json()["enrolled"] is False  # one failure counts, nothing was spent before

    def test_spoofed_forwarded_for_does_not_open_a_new_bucket(self, client, db, exam, roster):
        for i in range(FAILS_PER_MINUTE.count):
            response = client.post(
                f"/api/v1/exams/by-code/{CODE}/check-student",
                json={"student_id": f"TE-9{i:02d}"},
                headers={"X-Real-IP": "203.0.113.7", "X-Forwarded-For": f"10.{i}.0.1, 10.{i}.0.2"},
            )
            assert response.status_code == 200
        refused = client.post(
            f"/api/v1/exams/by-code/{CODE}/check-student",
            json={"student_id": "TE-001"},
            headers={"X-Real-IP": "203.0.113.7", "X-Forwarded-For": "192.0.2.99"},
        )
        assert refused.status_code == 429

    def test_ipv6_addresses_of_one_subscriber_share_a_bucket(self, client, db, exam, roster):
        for i in range(FAILS_PER_MINUTE.count):
            check(client, f"TE-9{i:02d}", ip=f"2001:db8:5:6:{i:x}::1")
        assert check(client, "TE-001", ip="2001:db8:5:6:ffff::9").status_code == 429
        assert check(client, "TE-001", ip="2001:db8:5:7::9").status_code == 200

    def test_the_total_volume_is_capped_far_above_a_class(self, client, db, exam, roster):
        ok = 0
        for n in range(ATTEMPTS_PER_MINUTE.count + 5):
            response = check(client, f"TE-{n % 30 + 1:03d}")
            if response.status_code == 429:
                break
            ok += 1
        assert ok == ATTEMPTS_PER_MINUTE.count
        assert response.status_code == 429 and "Retry-After" in response.headers


class TestCodeCircuitBreaker:
    def _spend_code_budget(self, client):
        """Many addresses, a few failures each: the distributed walk."""
        n = 0
        octet = 1
        while n < CODE_FAILS_SOFT.count:
            for k in range(5):
                check(client, f"TE-9{k:02d}", ip=f"198.51.100.{octet}")
                n += 1
            octet += 1

    def test_the_breaker_refuses_addresses_that_already_failed_but_not_new_ones(self, client, db, exam, roster):
        for i in range(SUSPECT_IP_FAILS):
            check(client, f"TE-9{i:02d}", ip="203.0.113.50")  # this address has failed 10 times
        self._spend_code_budget(client)
        assert check(client, "TE-001", ip="203.0.113.50").status_code == 429  # the suspect is refused
        fresh = check(client, "TE-001", ip="192.0.2.77")
        assert fresh.status_code == 200 and fresh.json()["enrolled"] is True  # a class student still enters

    def test_below_the_breaker_nobody_is_refused_for_ten_failures(self, client, db, exam, roster):
        for i in range(SUSPECT_IP_FAILS):
            check(client, f"TE-9{i:02d}", ip="203.0.113.50")
        assert check(client, "TE-001", ip="203.0.113.50").status_code == 200


class TestNoNameLeakage:
    def test_a_mismatch_returns_no_student_and_the_generic_text(self, client, db, exam, roster):
        body = check(client, "TE-9999").json()
        assert body["enrolled"] is False
        assert body["student"] is None
        assert body["message"] == GENERIC
        assert "auth-service" not in str(body) and "TE-9999" not in str(body)

    def test_start_refusal_is_the_same_generic_text(self, client, db, exam, roster):
        refused = start(client, "TE-9999")
        assert refused.status_code == 403
        assert refused.json()["detail"] == GENERIC

    def test_a_match_confirms_the_student_exactly_as_before(self, client, db, exam, roster):
        body = check(client, "te-007").json()
        assert body["enrolled"] is True and body["group_required"] is True
        assert body["student"]["full_name"] == "Фамилия7 Имя7"
        assert body["student"]["display_id"] == "TE-007"
        assert body["already_submitted"] is False

    def test_a_429_carries_no_roster_data(self, client, db, exam, roster):
        for i in range(FAILS_PER_MINUTE.count):
            check(client, f"TE-9{i:02d}")
        refused = check(client, "TE-007")
        assert refused.status_code == 429
        assert "Фамилия" not in refused.text and "student" not in refused.json()


# ── auth-service's refusal text never reaches the student ───────────────────────────────────────────────────

class _AuthReply:
    def __init__(self, payload, status_code=200):
        self._payload, self.status_code = payload, status_code

    def json(self):
        return self._payload


def _use_auth_reply(monkeypatch, payload, status=200):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            return _AuthReply(payload, status)

    monkeypatch.setattr(router.httpx, "AsyncClient", _Client)


AUTH_REFUSALS = [
    {"enrolled": False, "group_name": "8А", "message": "Ученик с ID «TE-031» не найден в группе «8А»."},
    {"enrolled": False, "group_name": "8А", "message": "Введённое имя (ФИО) или Student ID не соответствуют ученику в этой группе."},
    {"enrolled": False, "group_name": "8А", "message": "В группе несколько учеников с таким именем — введите ваш ID"},
    {"enrolled": False, "group_name": "8А", "message": "Несколько учеников подходят под этот ID — обратитесь к преподавателю."},
    {"enrolled": False, "group_name": "8А", "message": "Ученик с именем «Иванов Иван» не найден в группе «8А»."},
    {"enrolled": False, "group_name": "", "message": "Группа не найдена"},
]


class TestAuthRefusalsAreNormalised:
    @pytest.mark.parametrize("refusal", AUTH_REFUSALS)
    def test_every_refusal_reads_the_same(self, monkeypatch, refusal):
        _use_auth_reply(monkeypatch, refusal)
        exam = SimpleNamespace(group_id="g1")
        import asyncio

        with pytest.raises(HTTPException) as caught:
            asyncio.run(_REAL_CANONICAL(exam, student_id="TE-031", student_name=None, identifier="TE-031"))
        assert caught.value.status_code == 403
        assert caught.value.detail == GENERIC

    def test_a_student_object_without_an_id_is_a_refusal_too(self, monkeypatch):
        _use_auth_reply(monkeypatch, {"enrolled": True, "student": {"full_name": "Иванов Иван"}})
        import asyncio

        with pytest.raises(HTTPException) as caught:
            asyncio.run(_REAL_CANONICAL(SimpleNamespace(group_id="g1"), student_id="x", student_name=None))
        assert caught.value.detail == GENERIC

    def test_a_match_passes_the_student_through(self, monkeypatch):
        student = {"id": "u1", "full_name": "Иванов Иван", "display_id": "TE-031", "class_level": 8}
        _use_auth_reply(monkeypatch, {"enrolled": True, "student": student})
        import asyncio

        got = asyncio.run(_REAL_CANONICAL(SimpleNamespace(group_id="g1"), student_id="TE-031", student_name=None))
        assert got == student


# captured at import: the ``client`` fixtures replace router._canonical_student_for_exam
_REAL_CANONICAL = router._canonical_student_for_exam
