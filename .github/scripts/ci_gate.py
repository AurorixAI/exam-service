#!/usr/bin/env python3
"""Do the tests of this commit have to run again? (CI speed pack #140, A)

The same sha goes through the workflow twice: ``develop`` (stage), then ``main`` (production). When the sha that arrives on ``main`` has
already passed ALL its test jobs in a run of this workflow on ``develop``, the test jobs are skipped on ``main`` and the deploy starts at once.

    ci_gate.py gate    --repo OWNER/NAME --sha SHA --ref-name REF --expect-tests N [--test-jobs REGEX] [--ok-job NAME] [--workflow deploy.yml] [--api URL]
                       prints «skip_tests=true|false» and «reason=...» (to $GITHUB_OUTPUT when set), always exits 0
    ci_gate.py verdict --gate RESULT --skip true|false --tests RESULT [--guards RESULT]      exit 0 = the deploy may go on

Fail closed: any doubt (no token, API error, a partial list, several answers that do not agree, an unexpected job set) means «run the tests».
Only a push to ``main`` can skip; ``develop`` and every other branch always run them. Stdlib only.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
TEST_JOB = re.compile(r"^test( \(.*\))?$")
GATE_OK_BRANCH = "main"
SOURCE_BRANCH = "develop"


def decide(ref_name, sha, runs_answer, jobs_of, repo, expect_tests, test_re=TEST_JOB, ok_job="tests-ok"):
    """-> (skip_tests: bool, reason: str). ``runs_answer`` is the API answer for the runs of the workflow with this head sha;
    ``jobs_of(run_id)`` returns the API answer for the jobs of one run (or raises). Pure: no network here."""
    if ref_name != GATE_OK_BRANCH:
        return False, f"branch {ref_name!r} is not {GATE_OK_BRANCH!r}: the tests run"
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        return False, f"sha {sha!r} is not a full commit id: the tests run"
    if not isinstance(runs_answer, dict) or not isinstance(runs_answer.get("workflow_runs"), list):
        return False, "the list of runs has an unexpected shape: the tests run"
    runs = runs_answer["workflow_runs"]
    if runs_answer.get("total_count") != len(runs):
        return False, f"the list of runs is partial ({len(runs)} of {runs_answer.get('total_count')}): the tests run"
    rejected = []
    for run in sorted(runs, key=lambda r: r.get("created_at", ""), reverse=True):
        rid = run.get("id")
        if run.get("head_sha") != sha:
            rejected.append(f"run {rid}: another sha")
            continue
        if run.get("head_branch") != SOURCE_BRANCH or run.get("event") != "push":
            rejected.append(f"run {rid}: branch {run.get('head_branch')!r}, event {run.get('event')!r}")
            continue
        head_repo = (run.get("head_repository") or {}).get("full_name")
        if head_repo and head_repo != repo:
            rejected.append(f"run {rid}: another repository {head_repo}")
            continue
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            rejected.append(f"run {rid}: {run.get('status')}/{run.get('conclusion')}")
            continue
        try:
            answer = jobs_of(rid)
        except Exception as error:                                  # network, JSON, anything: not proof
            rejected.append(f"run {rid}: jobs unreadable ({type(error).__name__})")
            continue
        problem = check_jobs(answer, expect_tests, test_re, ok_job)
        if problem:
            rejected.append(f"run {rid}: {problem}")
            continue
        return True, f"sha {sha} прошёл тесты в run {rid} (develop)"
    return False, "no run of this sha on develop proves the tests (" + ("; ".join(rejected) or "no run found") + "): the tests run"


def check_jobs(answer, expect_tests, test_re=TEST_JOB, ok_job="tests-ok"):
    """None when every test job (and ``ok_job``) of the run succeeded and there are exactly ``expect_tests`` test jobs; else the reason."""
    if not isinstance(answer, dict) or not isinstance(answer.get("jobs"), list):
        return "unexpected shape of the jobs"
    jobs = answer["jobs"]
    if answer.get("total_count") != len(jobs):
        return f"partial list of jobs ({len(jobs)} of {answer.get('total_count')})"
    tests = [j for j in jobs if test_re.match(j.get("name", ""))]
    if len(tests) != expect_tests or len({j["name"] for j in tests}) != expect_tests:
        return f"{len(tests)} test jobs, expected {expect_tests}"
    bad = [f"{j['name']}={j.get('conclusion')}" for j in tests if j.get("status") != "completed" or j.get("conclusion") != "success"]
    if bad:
        return "test jobs not green: " + ", ".join(bad)
    ok = [j for j in jobs if j.get("name") == ok_job]
    if len(ok) != 1 or ok[0].get("conclusion") != "success":
        return f"{ok_job} is not green"
    return None


def verdict(gate_result, skip, tests_result, guards_result="success"):
    """-> (ok: bool, message). The one rule of the ``tests-ok`` job that guards the deploy.

    «Skipped» is believed only when the gate job itself succeeded AND the test jobs really did not run. In every other case (the gate failed or
    said nothing, it said «run») the test jobs must have run and passed: a broken gate costs time, never safety. The tests of the deploy guards
    themselves (``guards``) always run and always have to pass."""
    if guards_result != "success":
        return False, f"the deploy guards job is {guards_result}: no deploy"
    if skip == "true":
        if gate_result != "success":
            return False, f"the gate output says skip but the gate job is {gate_result}: no deploy"
        if tests_result != "skipped":
            return False, f"the gate skipped the tests but the test job is {tests_result}: no deploy"
        return True, "tests skipped by the gate (the same sha passed them on develop)"
    if tests_result != "success":
        return False, f"the test jobs are {tests_result}: no deploy"
    return True, "all test jobs passed"


def fetch(url, token, timeout=20):
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "algo-ci-gate"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def emit(skip, reason):
    line = f"skip_tests={'true' if skip else 'false'}"
    print(line)
    print(reason)
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write(line + "\n" + "reason=" + reason.replace("\n", " ") + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--repo", required=True)
    g.add_argument("--sha", required=True)
    g.add_argument("--ref-name", required=True)
    g.add_argument("--expect-tests", type=int, required=True)
    g.add_argument("--test-jobs", default=TEST_JOB.pattern, help="regex of the names of the test jobs")
    g.add_argument("--ok-job", default="tests-ok", help="name of the job that sums the tests up")
    g.add_argument("--workflow", default="deploy.yml")
    g.add_argument("--api", default=API)
    v = sub.add_parser("verdict")
    v.add_argument("--gate", required=True)
    v.add_argument("--skip", default="")
    v.add_argument("--tests", required=True)
    v.add_argument("--guards", default="success")
    args = parser.parse_args(argv)

    if args.cmd == "verdict":
        ok, message = verdict(args.gate, args.skip, args.tests, args.guards)
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1

    if args.ref_name != GATE_OK_BRANCH:
        emit(False, f"branch {args.ref_name!r}: the tests always run")
        return 0
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        emit(False, "no token for the API: the tests run")
        return 0
    base = f"{args.api}/repos/{args.repo}/actions"
    try:
        runs = fetch(f"{base}/workflows/{args.workflow}/runs?head_sha={args.sha}&per_page=100", token)
        skip, reason = decide(args.ref_name, args.sha, runs, lambda rid: fetch(f"{base}/runs/{rid}/jobs?per_page=100", token),
                              args.repo, args.expect_tests, re.compile(args.test_jobs), args.ok_job)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as error:
        skip, reason = False, f"the API did not answer clearly ({type(error).__name__}): the tests run"
    emit(skip, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
