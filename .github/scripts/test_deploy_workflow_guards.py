#!/usr/bin/env python3
"""Execute this repository's real deployment guards with synthetic Docker output."""
from pathlib import Path
import os
import re
import subprocess
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows" / "deploy.yml"
SOURCE_SHA = "a" * 40


def step_run(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + name) + 1
    end = next((i for i in range(start, len(lines))
                if lines[i].startswith("      - ")), len(lines))
    run = next(i for i in range(start, end) if lines[i] == "        run: |") + 1
    body = [line[10:] if line.startswith("          ") else line for line in lines[run:end]]
    return "\n".join(body).rstrip()


def remote_guard(name):
    run = step_run(name)
    body = run.split('"$SERVER_USER@$SERVER_HOST" "', 1)[1].rsplit('\n"', 1)[0]
    body = body.replace('\\"', '"').replace('\\$', '$')
    return re.sub(r"\$\{\{.*?\}\}", SOURCE_SHA, body)


def metadata_command():
    line = next(line for line in step_run("Export deployed release manifest").splitlines()
                if line.startswith("METADATA="))
    start = line.index('"docker inspect ') + 1
    end = line.rindex('")"')
    return re.sub(r"\$\{\{.*?\}\}", SOURCE_SHA, line[start:end])


STUB = r'''
docker() {
  if [ "$1" = image ]; then
    printf '%s\n' "$TEST_IMAGE_REVISION"
  elif [ "$1" = inspect ]; then
    case "$*" in
      *'.State.Health'*)
        [ "$TEST_HEALTH" != missing ] || return 1
        printf '%s\n' "$TEST_HEALTH" ;;
      *'.State.Running'*)
        [ "$TEST_HEALTH" != missing ] || return 1
        printf '%s\n' "$TEST_RUNNING" ;;
      *'.Config.Env'*)
        printf '%s\n' "SECRET_KEY=synthetic-value-must-stay-on-server" \
          "DATABASE_URL=synthetic-private-database" "RELEASE_SHA=$TEST_SOURCE_SHA" \
          "RELEASE_REF=develop" "RELEASED_AT=synthetic-time" ;;
      *'.Image'*) printf '%s\n' 'sha256:synthetic-image-id' ;;
      *) return 88 ;;
    esac
  elif [ "$1" = logs ]; then
    return 0
  else
    return 88
  fi
}
sleep() { :; }
'''


def execute(script, *, health="healthy", running="true", image_revision=SOURCE_SHA):
    env = dict(os.environ, TEST_HEALTH=health, TEST_RUNNING=running,
               TEST_IMAGE_REVISION=image_revision, TEST_SOURCE_SHA=SOURCE_SHA)
    return subprocess.run(["bash"], input=STUB + script, text=True,
                          capture_output=True, env=env, timeout=5)


class DeploymentGuardTests(unittest.TestCase):
    def test_healthy_running_container_is_accepted(self):
        self.assertEqual(execute(remote_guard("Verify container is healthy")).returncode, 0)

    def test_stopped_container_with_last_healthy_state_is_rejected(self):
        result = execute(remote_guard("Verify container is healthy"), running="false")
        self.assertNotEqual(result.returncode, 0)

    def test_starting_container_is_not_accepted(self):
        result = execute(remote_guard("Verify container is healthy"), health="starting")
        self.assertNotEqual(result.returncode, 0)

    def test_missing_container_is_rejected(self):
        result = execute(remote_guard("Verify container is healthy"), health="missing")
        self.assertNotEqual(result.returncode, 0)

    def test_metadata_crossing_ssh_contains_only_release_fields(self):
        result = execute(metadata_command())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["RELEASE_SHA=" + SOURCE_SHA,
                         "RELEASE_REF=develop", "RELEASED_AT=synthetic-time"])

    def test_matching_running_image_revision_is_accepted(self):
        result = execute(remote_guard("Verify deployed revision"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_correct_runtime_env_cannot_hide_wrong_image_revision(self):
        result = execute(remote_guard("Verify deployed revision"), image_revision="b" * 40)
        self.assertNotEqual(result.returncode, 0)

    def test_missing_image_revision_is_rejected(self):
        result = execute(remote_guard("Verify deployed revision"), image_revision="")
        self.assertNotEqual(result.returncode, 0)


# --- "Verify site responds": the step is executed for real against a local HTTP
# server (real curl; for services the real python one-liner that the step runs
# inside the container, with ssh/docker/sleep stubbed). ------------------------
import http.server
import json
import threading


class FakeSite(http.server.BaseHTTPRequestHandler):
    state = {"sha": SOURCE_SHA, "version_code": 200, "login_code": 200, "calls": 0}

    def do_GET(self):
        st = self.state
        st["calls"] += 1
        if self.path == "/api/version" or self.path == "/version":
            body = json.dumps({"release_sha": st["sha"]}).encode()
            self.send_response(st["version_code"])
        elif self.path == "/login":
            body = b"login"
            self.send_response(st["login_code"])
        else:  # "/" redirects to the login, like the real site
            body = b""
            self.send_response(307)
            self.send_header("Location", "/login")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def verify_step():
    return step_run("Verify site responds")


def run_verify(extra_stubs="", extra_env=None, **state):
    FakeSite.state.update({"sha": SOURCE_SHA, "version_code": 200, "login_code": 200, "calls": 0})
    FakeSite.state.update(state)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    env = dict(os.environ, SITE="http://127.0.0.1:%d" % port, EXPECTED_SHA=SOURCE_SHA,
               SERVICE_PORT=str(port), SERVER_USER="algo", SERVER_HOST="host.invalid",
               NO_PROXY="*", no_proxy="*", HOME=os.environ.get("HOME", "/tmp"))
    env.update(extra_env or {})
    try:
        # GitHub runs a step as `bash -e`; do the same.
        return subprocess.run(["bash", "-e"], input="sleep() { :; }\n" + extra_stubs + verify_step(),
                              text=True, capture_output=True, env=env, timeout=20)
    finally:
        server.shutdown()
        server.server_close()


class VerifySiteStaticTests(unittest.TestCase):
    def test_step_exists_and_compares_the_release_sha(self):
        step = verify_step()
        self.assertIn("EXPECTED_SHA", step)
        self.assertIn("release_sha", step)
        self.assertRegex(WORKFLOW.read_text(), r"EXPECTED_SHA: \$\{\{ github\.sha \}\}")

    def test_step_checks_the_public_login_not_the_redirecting_root(self):
        step = verify_step()
        self.assertIn('"$SITE/login"', step)
        self.assertNotIn('"$SITE/"', step)
        self.assertNotIn("never returned 200", step)

    def test_every_network_call_has_a_limit(self):
        step = verify_step()
        for line in step.splitlines():
            if "curl " in line and not line.lstrip().startswith("#"):
                self.assertIn("--max-time", line, line)
        self.assertEqual(len(re.findall(r"\bseq 1 12\b", step)), step.count("for attempt"))


# ssh is replaced by a function that refuses to run without the four limits of
# rule 57 and then runs the remote command locally; `timeout` is passed through;
# `docker exec <container> python -c <code>` runs the real one-liner with python3.
SSH_STUBS = r"""
ssh() {
  case "$*" in
    *ConnectTimeout=10*ServerAliveInterval=10*ServerAliveCountMax=2*BatchMode=yes*) ;;
    *) echo "ssh without limits: $*" >&2; return 97 ;;
  esac
  [ "${TEST_SSH_RC:-0}" = 0 ] || return "$TEST_SSH_RC"
  eval "${@: -1}"
}
timeout() { shift; "$@"; }
docker() {
  [ "$1" = exec ] || return 88
  [ "$2" = "algo-test-container" ] || { echo "wrong container: $2" >&2; return 89; }
  shift 2
  "$@"
}
python() { python3 "$@"; }
"""


class VerifySiteServiceTests(unittest.TestCase):
    def verify(self, **state):
        env = state.pop("extra_env", {})
        return run_verify(SSH_STUBS, dict(CONTAINER="algo-test-container", **env), **state)

    def test_matching_release_and_login_200_pass(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release_sha " + SOURCE_SHA, result.stdout)

    def test_old_release_fails_with_both_versions_named(self):
        result = self.verify(sha="b" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("версия " + "b" * 40 + ", ожидали " + SOURCE_SHA, result.stderr)

    def test_unknown_release_fails(self):
        result = self.verify(sha="unknown")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("версия unknown", result.stderr)

    def test_service_not_answering_fails_with_the_exit_code(self):
        result = self.verify(version_code=500)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("не ответил на /version", result.stderr)

    def test_ssh_failure_terminates_with_a_message(self):
        result = self.verify(extra_env={"TEST_SSH_RC": "255"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("код 255", result.stderr)

    def test_login_redirect_fails_with_the_code(self):
        result = self.verify(login_code=307)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/login ответил 307", result.stderr)

    def test_ssh_carries_every_limit(self):
        step = verify_step()
        for option in ("ConnectTimeout=10", "ServerAliveInterval=10",
                       "ServerAliveCountMax=2", "BatchMode=yes", "timeout 20 docker exec"):
            self.assertIn(option, step)

    def test_service_port_is_the_one_the_image_listens_on(self):
        dockerfile = (WORKFLOW.parents[2] / "Dockerfile").read_text()
        port = re.search(r"^\s*SERVICE_PORT: \"(\d+)\"", WORKFLOW.read_text(), re.M).group(1)
        self.assertRegex(dockerfile, r"EXPOSE " + port + r"\b")


if __name__ == "__main__":
    unittest.main()
