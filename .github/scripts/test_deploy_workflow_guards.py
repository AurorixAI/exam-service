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


if __name__ == "__main__":
    unittest.main()
