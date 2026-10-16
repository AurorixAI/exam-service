"""#33: the public exam entry must not be a roster oracle.

Anyone holding an exam code can ask «is TE-031 in the group?» and gets a name back on a match.  The answer stays
(the page shows the confirmed student) but walking the ID space is made expensive: a strict budget of DISTINCT
FAILED inputs per (exam code, client address) taken atomically at admission and given back on a match, a per-code
circuit breaker aimed at the address that walks, the teacher's reset, a generic refusal text, 429 + Retry-After.  A
whole class behind one school NAT must still get in.
"""
from __future__ import annotations

import os
import sys
import threading

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
    ADDED,
    ATTEMPTS_PER_MINUTE,
    BREAKER_MIN_DISTINCT_FAILS,
    CODE_FAILS_SOFT,
    EXISTS,
    FAILS_PER_HOUR,
    FAILS_PER_MINUTE,
    FULL,
    admit_identity_attempt,
    client_ip,
    code_key,
    counters,
    identity_key,
    limit_bucket,
    reset_entry_limits,
)
from src.models.exam_models import GeneratedExam
from tests.conftest import make_actor, make_exam_in_db, make_task

CODE = "EXRATE0001"
OTHER_CODE = "EXRATE0002"
GENERIC = "Ученик не найден в этой группе — проверьте имя или ID"
TOO_MANY = "Слишком много попыток. Подождите минуту и попробуйте снова."
TASKS = [make_task(f"TASK_{i:03d}", answer=str(i)) for i in range(1, 3)]


def miss(n: int) -> str:
    """The n-th DISTINCT wrong ID (the budget counts distinct failed inputs)."""
    return f"NOPE-{n:05d}"


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


def spend_minute_budget(client, ip="203.0.113.7", code=CODE):
    for n in range(FAILS_PER_MINUTE.count):
        assert check(client, miss(n), ip=ip, code=code).status_code == 200


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

    def test_retyping_the_same_input_is_the_same_key(self):
        assert identity_key("TE-031", None, "TE-031") == identity_key(" te-031 ", None, "TE-031".lower())
        assert identity_key("TE-031", "Иванов  Иван", "x") == identity_key("TE-031", "иванов иван", "x")
        assert identity_key("TE-031", None, None) != identity_key("TE-032", None, None)
        assert identity_key("a", "b", None) != identity_key("a", None, "b")  # which field the text was typed in matters
        assert len(identity_key("x" * 100_000)) == len(identity_key("a"))


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

    def test_a_set_counts_distinct_members_up_to_its_limit(self, monkeypatch):
        clock = _Clock(monkeypatch)
        assert counters.add_member("s", "a", 60, 2) == (ADDED, 1, 60)
        assert counters.add_member("s", "b", 60, 2)[0] == ADDED
        assert counters.add_member("s", "a", 60, 2)[0] == EXISTS  # the same member counts once
        status, size, left = counters.add_member("s", "c", 60, 2)
        assert (status, size) == (FULL, 2) and left == 60
        assert counters.count_members("s") == (2, 60)
        counters.remove_member("s", "a")
        assert counters.add_member("s", "c", 60, 2)[0] == ADDED
        clock.now += 61
        assert counters.count_members("s") == (0, 0)
        assert counters.add_member("s", "z", 60, 2)[0] == ADDED  # a new window

    def test_a_member_that_is_already_there_is_let_through_even_when_the_set_is_full(self):
        counters.add_member("s", "a", 60, 1)
        assert counters.add_member("s", "a", 60, 1)[0] == EXISTS
        assert counters.add_member("s", "b", 60, 1)[0] == FULL

    def test_epochs_start_at_zero_and_only_grow(self):
        assert counters.epoch("scope") == 0
        assert counters.bump_epoch("scope") == 1
        assert counters.bump_epoch("scope") == 2
        assert counters.epoch("scope") == 2 and counters.epoch("other") == 0

    def test_memory_does_not_grow_without_bound(self, monkeypatch):
        monkeypatch.setattr(abuse_limit, "_MEMORY_SOFT_LIMIT", 50)
        monkeypatch.setattr(abuse_limit, "_MEMORY_HARD_LIMIT", 100)
        for i in range(1000):
            counters.hit(f"flood:{i}", 3600)
            counters.add_member(f"flood-set:{i}", "m", 3600, 5)
        assert len(counters._memory) <= 100 and len(counters._sets) <= 100


class _FakeRedis:
    """Just enough of redis-py for the limiter: eval of its two scripts, pipelines, srem, get/incr/expire."""

    def __init__(self):
        self.values, self.sets, self.ttls, self.evals = {}, {}, {}, 0

    def eval(self, script, numkeys, key, *args):
        self.evals += 1
        if script is abuse_limit._HIT_LUA:
            window = args[0]
            self.values[key] = self.values.get(key, 0) + 1
            if self.values[key] == 1:
                self.ttls[key] = int(window)
            return [self.values[key], self.ttls[key]]
        member, window, limit = args
        members = self.sets.setdefault(key, set())
        if member in members:
            return [0, len(members), self.ttls.get(key, -1)]
        if len(members) >= int(limit):
            return [2, len(members), self.ttls.get(key, -2)]
        members.add(member)
        self.ttls.setdefault(key, int(window))
        return [1, len(members), self.ttls[key]]

    def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    def get(self, key):
        return str(self.values[key]) if key in self.values else None

    def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key, seconds):
        self.ttls[key] = seconds

    def pipeline(self):
        outer = self

        class _Pipe:
            def __init__(self):
                self.calls = []

            def get(self, key):
                self.calls.append(("get", key))

            def scard(self, key):
                self.calls.append(("scard", key))

            def ttl(self, key):
                self.calls.append(("ttl", key))

            def execute(self):
                out = []
                for op, key in self.calls:
                    if op == "get":
                        out.append(outer.get(key))
                    elif op == "scard":
                        out.append(len(outer.sets.get(key, ())))
                    else:
                        out.append(outer.ttls.get(key, -2))
                return out

        return _Pipe()


class _BrokenRedis:
    def eval(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    def pipeline(self):
        raise ConnectionError("redis is down")

    def get(self, *args):
        raise ConnectionError("redis is down")


class TestRedisBackend:
    def test_counts_live_in_redis_when_it_is_there(self):
        fake = _FakeRedis()
        counters.use_redis(fake)
        assert counters.hit("k", 60) == (1, 60)
        assert counters.hit("k", 60) == (2, 60)
        assert counters.peek("k") == (2, 60)
        assert counters.peek("absent") == (0, 0)
        assert counters.add_member("s", "a", 60, 2)[0] == ADDED
        assert counters.add_member("s", "a", 60, 2)[0] == EXISTS
        assert counters.add_member("s", "b", 60, 2)[0] == ADDED
        assert counters.add_member("s", "c", 60, 2)[0] == FULL
        assert counters.count_members("s") == (2, 60)
        counters.remove_member("s", "b")
        assert counters.count_members("s")[0] == 1
        assert counters.bump_epoch("x") == 1 and counters.epoch("x") == 1
        assert not counters._memory and not counters._sets and not counters._epochs

    def test_redis_down_falls_back_to_memory_with_one_warning(self, caplog):
        counters.use_redis(_BrokenRedis())
        with caplog.at_level("WARNING", logger="exam_service.abuse_limit"):
            for _ in range(5):
                counters.hit("k", 60)
            counters.add_member("s", "a", 60, 5)
            counters.bump_epoch("x")
        assert counters.peek("k")[0] == 5  # still counted, in process memory
        assert counters.epoch("x") == 1
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
    def test_real_redis_scripts(self):
        import redis

        client = redis.from_url(os.environ["RATE_LIMIT_REDIS_URL"], decode_responses=True)
        keys = ["rlt:k", "rlt:s", "rlt:epoch"]
        client.delete(*keys, "examepoch:rlt")
        counters.use_redis(client)
        try:
            assert counters.hit("rlt:k", 60)[0] == 1
            count, ttl = counters.hit("rlt:k", 60)
            assert count == 2 and 0 < ttl <= 60
            assert counters.peek("rlt:k")[0] == 2
            assert counters.peek("rlt:none") == (0, 0)
            assert counters.add_member("rlt:s", "a", 60, 2)[0] == ADDED
            assert counters.add_member("rlt:s", "a", 60, 2)[0] == EXISTS
            assert counters.add_member("rlt:s", "b", 60, 2)[0] == ADDED
            status, size, ttl = counters.add_member("rlt:s", "c", 60, 2)
            assert (status, size) == (FULL, 2) and 0 < ttl <= 60
            counters.remove_member("rlt:s", "a")
            assert counters.count_members("rlt:s")[0] == 1
            assert counters.count_members("rlt:nothing") == (0, 0)
            assert counters.bump_epoch("rlt") == 1 and counters.epoch("rlt") == 1
            assert 0 < client.ttl("examepoch:rlt") <= abuse_limit._EPOCH_TTL
        finally:
            client.delete(*keys, "examepoch:rlt")


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
        for i in range(1, 6):
            assert check(client, f"TE-9{i:02d}").json()["enrolled"] is False  # 5 typos
        for i in range(1, 31):
            sid = f"TE-{i:03d}"
            assert check(client, sid).status_code == 200
            assert start(client, sid).status_code == 200

    def test_every_student_of_a_class_mistyping_once_gets_in_when_the_minute_is_over(self, client, db, exam, roster, monkeypatch):
        """30 distinct typos in one minute are exactly the budget: the retries wait out the minute, then all enter."""
        clock = _Clock(monkeypatch)
        for i in range(1, 31):
            assert check(client, miss(i)).json()["enrolled"] is False
        waiting = check(client, "TE-001")
        assert waiting.status_code == 429 and int(waiting.headers["Retry-After"]) <= 60
        clock.now += 61
        for i in range(1, 31):
            assert check(client, f"TE-{i:03d}").json()["enrolled"] is True  # and each success gives its provisional back

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
        spend_minute_budget(client)
        assert check(client, miss(999)).status_code == 429  # the first class's budget at this address is spent...
        other = check(client, "TE-001", code=OTHER_CODE)  # ...the second class (same NAT) is untouched
        assert other.status_code == 200 and other.json()["enrolled"] is True


class TestFailedLookupsAreBudgeted:
    def test_the_thirty_first_distinct_failure_in_a_minute_is_refused_with_retry_after(self, client, db, exam, roster):
        spend_minute_budget(client)
        refused = check(client, miss(500))
        assert refused.status_code == 429
        assert refused.json()["detail"] == TOO_MANY
        assert 1 <= int(refused.headers["Retry-After"]) <= 60

    def test_retyping_the_same_wrong_input_counts_once(self, client, db, exam, roster):
        for _ in range(60):
            body = check(client, "TE-9999")
            assert body.status_code == 200 and body.json()["enrolled"] is False
        for n in range(FAILS_PER_MINUTE.count - 1):  # 29 more distinct ones fit beside the one that was retyped
            assert check(client, miss(n)).status_code == 200
        assert check(client, miss(900)).status_code == 429

    def test_a_spelling_of_the_same_input_counts_once(self, client, db, exam, roster):
        for spelling in ("TE-9999", " te-9999 ", "Te-9999"):
            assert check(client, spelling).status_code == 200
        counters_after = counters.count_members(f"examfail:60:{code_key(CODE)}:0:203.0.113.7")
        assert counters_after[0] == 1

    def test_the_right_id_is_refused_too_while_the_budget_is_spent(self, client, db, exam, roster):
        spend_minute_budget(client)
        assert check(client, "TE-001").status_code == 429

    def test_another_address_is_not_affected(self, client, db, exam, roster):
        spend_minute_budget(client)
        assert check(client, "TE-001").status_code == 429
        assert check(client, "TE-001", ip="198.51.100.20").status_code == 200

    def test_the_budget_comes_back_after_the_window(self, client, db, exam, roster, monkeypatch):
        clock = _Clock(monkeypatch)
        spend_minute_budget(client)
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
                assert check(client, miss(spent)).status_code == 200
                spent += 1
            clock.now += 61  # a fresh minute, the hour goes on
        refused = check(client, miss(10_000))
        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) > 60
        assert "мин" in refused.json()["detail"] and refused.json()["detail"] != TOO_MANY

    def test_a_walk_over_the_ids_is_stopped_after_the_budget(self, client, db, exam, roster):
        """The attacker's currency is distinct failed guesses: of 200 IDs only a handful are read before the 429."""
        read, refused = 0, False
        for n in range(1, 201):
            response = check(client, f"TE-{n:03d}" if n <= 40 and n % 3 == 0 else miss(n))
            if response.status_code == 429:
                refused = True
                break
            read += 1 if response.json()["enrolled"] else 0
        assert refused and read <= 13

    def test_start_failures_spend_the_same_budget(self, client, db, exam, roster):
        for n in range(FAILS_PER_MINUTE.count):
            assert start(client, miss(n)).status_code == 403
        assert start(client, "TE-001").status_code == 429
        assert check(client, "TE-001").status_code == 429

    def test_a_success_does_not_spend_the_failure_budget(self, client, db, exam, roster):
        for _ in range(2):
            for i in range(1, 31):
                assert check(client, f"TE-{i:03d}").json()["enrolled"] is True
        for n in range(FAILS_PER_MINUTE.count):  # the whole budget is still there
            assert check(client, miss(n)).status_code == 200

    def test_a_410_does_not_spend_the_failure_budget(self, client, db, exam, roster):
        exam.status = "closed"
        db.commit()
        for n in range(FAILS_PER_MINUTE.count * 2):
            assert check(client, miss(n)).status_code == 410
        exam.status = "active"
        db.commit()
        assert check(client, miss(0)).status_code == 200  # nothing was counted against the address
        spend = [check(client, miss(n)).status_code for n in range(1, FAILS_PER_MINUTE.count)]
        assert spend == [200] * (FAILS_PER_MINUTE.count - 1)

    def test_spoofed_forwarded_for_does_not_open_a_new_bucket(self, client, db, exam, roster):
        for i in range(FAILS_PER_MINUTE.count):
            response = client.post(
                f"/api/v1/exams/by-code/{CODE}/check-student",
                json={"student_id": miss(i)},
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
            check(client, miss(i), ip=f"2001:db8:5:6:{i:x}::1")
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


class TestRetryAfterIsTheLongestWait:
    def test_minute_and_hour_exceeded_together_answer_with_the_hour(self, client, db, exam, roster, monkeypatch):
        clock = _Clock(monkeypatch)
        spent = 0
        while spent < FAILS_PER_HOUR.count:
            for _ in range(FAILS_PER_MINUTE.count):
                if spent == FAILS_PER_HOUR.count:
                    break
                check(client, miss(spent))
                spent += 1
            if spent < FAILS_PER_HOUR.count:
                clock.now += 61
        # the last minute is full (30 distinct) AND the hour is full (150): the wait is the hour's, not the minute's
        refused = check(client, miss(20_000))
        assert refused.status_code == 429
        assert int(refused.headers["Retry-After"]) > 3000

    def test_flood_and_failures_exceeded_together_answer_with_the_longer(self, monkeypatch):
        clock = _Clock(monkeypatch)
        request = _req({"X-Real-IP": "203.0.113.7"})
        for n in range(FAILS_PER_MINUTE.count):
            admit_identity_attempt(request, CODE, miss(n))
        clock.now += 20
        with pytest.raises(HTTPException) as caught:
            admit_identity_attempt(request, CODE, miss(999))
        assert caught.value.status_code == 429
        assert int(caught.value.headers["Retry-After"]) == 40  # the minute window opened 20 s ago


class TestTheProvisionalFailureIsAtomic:
    def test_parallel_admissions_cannot_exceed_the_budget(self):
        """200 threads with 200 distinct inputs against a budget of 30 per minute: exactly 30 are admitted."""
        request = _req({"X-Real-IP": "203.0.113.7"})
        admitted, refused, errors = [], [], []
        barrier = threading.Barrier(50)

        def worker(base):
            try:
                barrier.wait()
                for n in range(base, base + 4):
                    try:
                        admitted.append(admit_identity_attempt(request, CODE, miss(n)))
                    except HTTPException as exc:
                        refused.append(exc.status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i * 4,)) for i in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert len(admitted) == FAILS_PER_MINUTE.count and set(refused) == {429}
        assert len(admitted) + len(refused) == 200

    def test_a_match_gives_the_provisional_failure_back(self):
        request = _req({"X-Real-IP": "203.0.113.7"})
        key = f"examfail:60:{code_key(CODE)}:0:203.0.113.7"
        ticket = admit_identity_attempt(request, CODE, "TE-001")
        assert counters.count_members(key)[0] == 1  # taken at admission
        ticket.close()  # the lookup found a student: no failure was called
        assert counters.count_members(key) == (0, 0)
        ticket.close()  # idempotent

    def test_a_failure_stays_counted(self):
        request = _req({"X-Real-IP": "203.0.113.7"})
        key = f"examfail:60:{code_key(CODE)}:0:203.0.113.7"
        ticket = admit_identity_attempt(request, CODE, "NOBODY")
        ticket.fail()
        ticket.close()
        assert counters.count_members(key)[0] == 1

    def test_a_refused_admission_leaves_nothing_behind(self):
        request = _req({"X-Real-IP": "203.0.113.7"})
        key_minute = f"examfail:60:{code_key(CODE)}:0:203.0.113.7"
        key_hour = f"examfail:3600:{code_key(CODE)}:0:203.0.113.7"
        for n in range(FAILS_PER_MINUTE.count):
            admit_identity_attempt(request, CODE, miss(n)).fail()
        with pytest.raises(HTTPException):
            admit_identity_attempt(request, CODE, "one-more")
        assert counters.count_members(key_minute)[0] == FAILS_PER_MINUTE.count
        assert counters.count_members(key_hour)[0] == FAILS_PER_MINUTE.count  # not rolled forward by the refusal


class TestCodeCircuitBreaker:
    def _arm(self, n=CODE_FAILS_SOFT.count + 5):
        """A distributed walk: many addresses, a few distinct failures each."""
        for k in range(n):
            ticket = admit_identity_attempt(_req({"X-Real-IP": f"198.51.100.{k % 200 + 1}"}), CODE, miss(5000 + k))
            ticket.fail()

    def test_an_address_that_is_walking_is_refused_once_the_code_is_under_attack(self, monkeypatch):
        clock = _Clock(monkeypatch)
        walker = _req({"X-Real-IP": "203.0.113.50"})
        for n in range(BREAKER_MIN_DISTINCT_FAILS):
            admit_identity_attempt(walker, CODE, miss(n)).fail()
        clock.now += 61  # a fresh minute: the minute budget is out of the picture, only the breaker can refuse
        admit_identity_attempt(walker, CODE, miss(900)).close()  # control: the code is not under attack, admitted
        self._arm()
        with pytest.raises(HTTPException) as caught:
            admit_identity_attempt(walker, CODE, miss(901))  # 30 distinct failures out of 31 attempts
        assert caught.value.status_code == 429
        assert int(caught.value.headers["Retry-After"]) > 3000  # until its hour of failures runs out

    def test_a_nat_with_a_few_typos_stays_in_even_when_the_code_is_under_attack(self, client, db, exam, roster):
        self._arm()
        for n in range(8):  # eight typos in the class
            assert check(client, miss(n)).json()["enrolled"] is False
        for i in range(1, 31):
            assert check(client, f"TE-{i:03d}").status_code == 200
            assert start(client, f"TE-{i:03d}").status_code == 200

    def test_failing_a_lot_is_not_enough_when_most_attempts_succeed(self, client, db, exam, roster, monkeypatch):
        clock = _Clock(monkeypatch)
        self._arm()
        for _ in range(3):  # 120 successful attempts of the class...
            for i in range(1, 41):
                assert check(client, f"TE-{i:03d}").status_code == 200
            clock.now += 61
        for n in range(BREAKER_MIN_DISTINCT_FAILS + 5):  # ...and 35 distinct failures: the failure share is 22%
            assert check(client, miss(n)).status_code == 200
            if n == 24:
                clock.now += 61
        assert check(client, "TE-001").status_code == 200

    def test_a_fresh_address_enters_while_the_breaker_is_armed(self, client, db, exam, roster):
        self._arm()
        fresh = check(client, "TE-001", ip="192.0.2.77")
        assert fresh.status_code == 200 and fresh.json()["enrolled"] is True

    def test_the_breaker_is_not_armed_by_few_failures(self):
        walker = _req({"X-Real-IP": "203.0.113.50"})
        for n in range(25):
            admit_identity_attempt(walker, CODE, miss(n)).fail()
        admit_identity_attempt(walker, CODE, miss(900)).close()  # the code has far fewer failures than the breaker needs


class TestTeacherResetsTheEntryLimits:
    URL = "/api/v1/exams/EXAM_TEST01/entry-limits/reset"

    def test_reset_clears_the_budget_of_the_address_and_the_breaker(self, client, db, exam, roster):
        spend_minute_budget(client)
        assert check(client, "TE-001").status_code == 429
        reset = client.post(self.URL)
        assert reset.status_code == 200 and reset.json() == {"exam_id": "EXAM_TEST01", "reset": True}
        assert check(client, "TE-001").status_code == 200
        spend_minute_budget(client)  # a full fresh budget, not what was left
        assert check(client, "TE-002").status_code == 429

    def test_reset_clears_every_address_of_the_code_but_not_another_code(self, client, db, exam, roster):
        for ip in ("203.0.113.1", "203.0.113.2"):
            spend_minute_budget(client, ip=ip)
        other_code = admit_identity_attempt(_req({"X-Real-IP": "203.0.113.1"}), OTHER_CODE, "x")
        other_code.fail()
        client.post(self.URL)
        for ip in ("203.0.113.1", "203.0.113.2"):
            assert check(client, "TE-001", ip=ip).status_code == 200
        epoch_other = counters.epoch(code_key(OTHER_CODE))
        assert epoch_other == 0  # the other exam's keys were not touched

    def test_reset_works_on_the_lobby_too_and_is_recorded(self, client, db, exam, roster):
        exam.status = "lobby"
        db.commit()
        assert client.post(self.URL).status_code == 200
        db.refresh(exam)
        assert exam.settings["lifecycle_events"][-1]["type"] == "entry_limits_reset"

    @pytest.mark.parametrize("status", ["draft", "variants_ready", "closed"])
    def test_a_closed_or_unopened_exam_has_nothing_to_reset(self, client, db, exam, roster, status):
        exam.status = status
        db.commit()
        assert client.post(self.URL).status_code == 409

    def test_an_exam_without_a_code_has_nothing_to_reset(self, client, db, exam, roster):
        exam.access_code = None
        db.commit()
        assert client.post(self.URL).status_code == 409

    def test_only_the_teacher_of_the_group_may_reset(self, client, db, exam, roster):
        from src.core.auth import require_exam_staff
        from src.main import app

        app.dependency_overrides[require_exam_staff] = lambda: make_actor("admin")
        assert client.post(self.URL).status_code == 403

    def test_an_unknown_exam_is_a_404(self, client, db, exam, roster):
        assert client.post("/api/v1/exams/NO_SUCH/entry-limits/reset").status_code == 404

    def test_reset_uses_a_new_epoch_so_old_keys_are_never_read(self):
        request = _req({"X-Real-IP": "203.0.113.7"})
        for n in range(FAILS_PER_MINUTE.count):
            admit_identity_attempt(request, CODE, miss(n)).fail()
        with pytest.raises(HTTPException):
            admit_identity_attempt(request, CODE, miss(999))
        assert reset_entry_limits(CODE) == 1
        admit_identity_attempt(request, CODE, miss(999))  # admitted at once, nothing was scanned or deleted
        assert counters.count_members(f"examfail:60:{code_key(CODE)}:0:203.0.113.7")[0] == FAILS_PER_MINUTE.count


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
