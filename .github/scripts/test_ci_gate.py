#!/usr/bin/env python3
"""The «skip the tests on main» decision (ci_gate.py): positive case, the negative cases the pack must prove, and the real command against a fake API."""
import http.server
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_gate  # noqa: E402

SHA = "a" * 40
OTHER = "b" * 40
REPO = "Owner/auth"
SHARDS = [f"test (shard {i}/8)" for i in range(1, 9)]
DEFAULT = object()


def run(rid=100, sha=SHA, branch="develop", event="push", status="completed", conclusion="success", created="2026-11-01T10:00:00Z", repo=REPO):
    return {"id": rid, "head_sha": sha, "head_branch": branch, "event": event, "status": status, "conclusion": conclusion,
            "created_at": created, "head_repository": {"full_name": repo}}


def runs_answer(*runs, total=None):
    return {"total_count": len(runs) if total is None else total, "workflow_runs": list(runs)}


def job(name, conclusion="success", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion}


def jobs_answer(names=tuple(SHARDS), bad=None, extra=(), total=None):
    jobs = [job(n, "failure" if n == bad else "success") for n in names] + [job("tests-ok")] + list(extra)
    return {"total_count": len(jobs) if total is None else total, "jobs": jobs}


class DecideTests(unittest.TestCase):
    def decide(self, ref="main", sha=SHA, runs=DEFAULT, jobs=DEFAULT, expect=8):
        runs = runs_answer(run()) if runs is DEFAULT else runs
        jobs = jobs_answer() if jobs is DEFAULT else jobs
        calls = []

        def jobs_of(rid):
            calls.append(rid)
            if isinstance(jobs, Exception):
                raise jobs
            return jobs
        result = ci_gate.decide(ref, sha, runs, jobs_of, REPO, expect)
        return result, calls

    def test_the_same_sha_green_on_develop_skips_the_tests_on_main(self):
        (skip, reason), _ = self.decide()
        self.assertTrue(skip)
        self.assertEqual(reason, f"sha {SHA} прошёл тесты в run 100 (develop)")

    def test_develop_and_other_branches_never_skip(self):
        for ref in ("develop", "CEO", "feature", ""):
            (skip, reason), calls = self.decide(ref=ref)
            self.assertFalse(skip, ref)
            self.assertEqual(calls, [], "no API call is needed to refuse")

    def test_a_sha_that_was_never_on_develop_runs_the_tests(self):
        (skip, reason), _ = self.decide(runs=runs_answer())
        self.assertFalse(skip)
        self.assertIn("no run found", reason)

    def test_a_run_of_another_sha_is_not_proof(self):
        (skip, _), calls = self.decide(runs=runs_answer(run(sha=OTHER)))
        self.assertFalse(skip)
        self.assertEqual(calls, [])

    def test_a_run_of_main_or_of_a_pull_request_is_not_proof(self):
        for kw in ({"branch": "main"}, {"branch": "CEO"}, {"event": "pull_request"}, {"event": "workflow_dispatch"}):
            (skip, _), _ = self.decide(runs=runs_answer(run(**kw)))
            self.assertFalse(skip, kw)

    def test_a_run_that_is_not_a_success_is_not_proof(self):
        for kw in ({"conclusion": "failure"}, {"conclusion": "cancelled"}, {"conclusion": None, "status": "in_progress"},
                   {"conclusion": "skipped"}, {"conclusion": "success", "status": "queued"}):
            (skip, _), _ = self.decide(runs=runs_answer(run(**kw)))
            self.assertFalse(skip, kw)

    def test_one_failed_shard_out_of_eight_is_not_proof(self):
        for shard in SHARDS:
            (skip, reason), _ = self.decide(jobs=jobs_answer(bad=shard))
            self.assertFalse(skip, shard)
            self.assertIn(shard, reason)

    def test_a_missing_or_skipped_shard_is_not_proof(self):
        (skip, _), _ = self.decide(jobs=jobs_answer(names=SHARDS[:7]))
        self.assertFalse(skip)
        skipped = jobs_answer()
        skipped["jobs"][3]["conclusion"] = "skipped"
        (skip, _), _ = self.decide(jobs=skipped)
        self.assertFalse(skip)
        (skip, _), _ = self.decide(jobs=jobs_answer(), expect=9)
        self.assertFalse(skip)

    def test_tests_ok_must_be_green(self):
        answer = jobs_answer()
        answer["jobs"][-1]["conclusion"] = "failure"
        (skip, _), _ = self.decide(jobs=answer)
        self.assertFalse(skip)
        answer["jobs"] = answer["jobs"][:-1]
        answer["total_count"] -= 1
        (skip, _), _ = self.decide(jobs=answer)
        self.assertFalse(skip)

    def test_a_partial_list_is_not_proof(self):
        (skip, _), _ = self.decide(runs=runs_answer(run(), total=2))
        self.assertFalse(skip)
        (skip, _), _ = self.decide(jobs=jobs_answer(total=40))
        self.assertFalse(skip)

    def test_an_unreadable_answer_is_not_proof(self):
        for broken in (None, [], {}, {"workflow_runs": "x"}, {"total_count": 1}):
            (skip, _), _ = self.decide(runs=broken)
            self.assertFalse(skip, broken)
        for broken in (None, {}, {"jobs": None}, OSError("timeout"), ValueError("not json")):
            (skip, _), _ = self.decide(jobs=broken)
            self.assertFalse(skip, broken)

    def test_a_short_or_odd_sha_is_not_proof(self):
        for sha in ("", "abc", "A" * 40, SHA[:-1]):
            (skip, _), _ = self.decide(sha=sha)
            self.assertFalse(skip, sha)

    def test_a_run_of_a_fork_is_not_proof(self):
        (skip, _), _ = self.decide(runs=runs_answer(run(repo="Stranger/auth")))
        self.assertFalse(skip)

    def test_the_newest_green_run_is_enough_even_if_an_older_one_failed(self):
        old_failed = run(rid=1, conclusion="failure", created="2026-11-01T09:00:00Z")
        (skip, reason), _ = self.decide(runs=runs_answer(old_failed, run(rid=2, created="2026-11-01T11:00:00Z")))
        self.assertTrue(skip)
        self.assertIn("run 2", reason)

    def test_a_single_job_workflow_and_a_custom_job_name(self):
        jobs = {"total_count": 2, "jobs": [job("build"), job("tests-ok")]}
        result = ci_gate.decide("main", SHA, runs_answer(run()), lambda rid: jobs, REPO, 1, re.compile(r"^build$"))
        self.assertTrue(result[0])
        result = ci_gate.decide("main", SHA, runs_answer(run()), lambda rid: jobs, REPO, 1)        # the default pattern does not see «build»
        self.assertFalse(result[0])
        single = {"total_count": 2, "jobs": [job("test"), job("tests-ok")]}
        self.assertTrue(ci_gate.decide("main", SHA, runs_answer(run()), lambda rid: single, REPO, 1)[0])


class VerdictTests(unittest.TestCase):
    def test_every_combination(self):
        table = {
            ("success", "false", "success"): True,
            ("success", "true", "skipped"): True,
            ("success", "false", "failure"): False,
            ("success", "false", "skipped"): False,        # the gate said «run» but nothing ran
            ("success", "false", "cancelled"): False,
            ("success", "true", "success"): False,         # the gate said «skip» but they ran: inconsistent
            ("success", "true", "failure"): False,
            ("success", "true", "cancelled"): False,
            ("success", "", "success"): True,              # no output from the gate: the tests ran and passed
            ("success", "", "skipped"): False,             # ... and if they did not run, no deploy
            ("failure", "", "success"): True,              # a broken gate costs time, not safety: the tests ran (test does not need the gate to succeed)
            ("failure", "", "failure"): False,
            ("failure", "", "skipped"): False,
            ("failure", "false", "success"): True,
            ("failure", "true", "skipped"): False,         # «skip» from a gate that failed is not believed
            ("skipped", "true", "skipped"): False,
            ("cancelled", "true", "skipped"): False,
            ("cancelled", "false", "failure"): False,
        }
        for (gate, skip, tests), expected in table.items():
            self.assertEqual(ci_gate.verdict(gate, skip, tests)[0], expected, (gate, skip, tests))

    def test_the_guards_job_must_be_green_in_every_case(self):
        for gate, skip, tests in (("success", "true", "skipped"), ("success", "false", "success"), ("failure", "", "success")):
            self.assertTrue(ci_gate.verdict(gate, skip, tests, "success")[0])
            for guards in ("failure", "cancelled", "skipped", ""):
                self.assertFalse(ci_gate.verdict(gate, skip, tests, guards)[0], (gate, skip, tests, guards))

    def test_the_command_exit_code(self):
        for argv, code in ((["verdict", "--gate", "success", "--skip", "true", "--tests", "skipped"], 0),
                           (["verdict", "--gate", "success", "--skip", "false", "--tests", "failure"], 1),
                           (["verdict", "--gate", "success", "--skip", "true", "--tests", "skipped", "--guards", "failure"], 1)):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(ci_gate.main(argv), code)


class FakeApi(http.server.BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def do_GET(self):
        FakeApi.seen.append((self.path, self.headers.get("Authorization")))
        path = self.path.split("?")[0]
        answer = FakeApi.routes.get(path)
        if answer is None:
            self.send_response(404)
            body = b"{}"
        elif answer == "boom":
            self.send_response(500)
            body = b"{}"
        else:
            self.send_response(200)
            body = json.dumps(answer).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class CommandAgainstFakeApiTests(unittest.TestCase):
    """The real ``gate`` command, its HTTP client and its output file, against a local server that plays the GitHub API."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.api = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def gate(self, routes, ref="main", token="t0ken", sha=SHA):
        FakeApi.routes = routes
        FakeApi.seen = []
        out = tempfile.NamedTemporaryFile("r", suffix=".out", delete=False)
        out.close()
        env = {"GITHUB_OUTPUT": out.name}
        if token:
            env["GITHUB_TOKEN"] = token
        old = {k: os.environ.get(k) for k in ("GITHUB_OUTPUT", "GITHUB_TOKEN")}
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.update(env)
        try:
            with redirect_stdout(io.StringIO()):
                code = ci_gate.main(["gate", "--repo", REPO, "--sha", sha, "--ref-name", ref, "--expect-tests", "8", "--api", self.api])
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
        text = Path(out.name).read_text()
        os.unlink(out.name)
        return code, text

    def good_routes(self):
        base = f"/repos/{REPO}/actions"
        return {f"{base}/workflows/deploy.yml/runs": runs_answer(run()), f"{base}/runs/100/jobs": jobs_answer()}

    def test_skip_is_written_for_the_workflow_to_read(self):
        code, text = self.gate(self.good_routes())
        self.assertEqual(code, 0)
        self.assertIn("skip_tests=true\n", text)
        self.assertIn(f"reason=sha {SHA} прошёл тесты в run 100 (develop)", text)
        self.assertTrue(all(auth == "Bearer t0ken" for _, auth in FakeApi.seen))
        self.assertIn(f"head_sha={SHA}", FakeApi.seen[0][0])

    def test_develop_does_not_even_call_the_api(self):
        code, text = self.gate(self.good_routes(), ref="develop")
        self.assertEqual((code, FakeApi.seen), (0, []))
        self.assertIn("skip_tests=false\n", text)

    def test_the_api_down_or_odd_means_the_tests_run(self):
        base = f"/repos/{REPO}/actions"
        for routes in ({}, {f"{base}/workflows/deploy.yml/runs": "boom"},
                       {f"{base}/workflows/deploy.yml/runs": runs_answer(run()), f"{base}/runs/100/jobs": "boom"},
                       {f"{base}/workflows/deploy.yml/runs": runs_answer(run()), f"{base}/runs/100/jobs": jobs_answer(bad=SHARDS[5])}):
            code, text = self.gate(routes)
            self.assertEqual(code, 0)
            self.assertIn("skip_tests=false\n", text, routes)

    def test_no_token_means_the_tests_run(self):
        code, text = self.gate(self.good_routes(), token="")
        self.assertEqual(code, 0)
        self.assertIn("skip_tests=false\n", text)
        self.assertEqual(FakeApi.seen, [])

    def test_a_server_that_is_not_there_means_the_tests_run(self):
        os.environ["GITHUB_TOKEN"] = "t"
        try:
            with redirect_stdout(io.StringIO()) as out:
                code = ci_gate.main(["gate", "--repo", REPO, "--sha", SHA, "--ref-name", "main", "--expect-tests", "8", "--api", "http://127.0.0.1:9"])
        finally:
            os.environ.pop("GITHUB_TOKEN", None)
        self.assertEqual(code, 0)
        self.assertIn("skip_tests=false", out.getvalue())


if __name__ == "__main__":
    unittest.main()
