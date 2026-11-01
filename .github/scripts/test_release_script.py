#!/usr/bin/env python3
"""Run the REAL remote script of the deploy step «Build the exact tested Git revision on server» with git and docker replaced by recorders.

What it proves (CI speed pack #140, B): a matching image is reused, every mismatch builds, a failed build deploys nothing, the guards of the
step (clean checkout, rollback tag, archive hash) are still there. The server is not touched."""
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKFLOW = HERE.parent / "workflows" / "deploy.yml"
SHA = "a" * 40
SERVICE_MARK = "io.algolab.service"
STEP = "Build the exact tested Git revision on server"
SRCDIR = "repo-src"
SERVICE = "svc"
PROJECT = "algo-staging"


def step_body():
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + STEP) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("      - ")), len(lines))
    body = [line[10:] if line.startswith("          ") else line for line in lines[start:end]]
    first = next(i for i, line in enumerate(body) if line.rstrip().endswith("<<'REMOTE'")) + 1
    last = next(i for i in range(first, len(body)) if body[i] == "REMOTE")
    return "\n".join(body[first:last]) + "\n"


FAKE_GIT = r"""#!/bin/bash
echo "git $*" >> "$FAKE_STATE/calls.log"
case "$1" in
  status) [ -z "$FAKE_DIRTY" ] || echo " M file" ;;
  rev-parse) echo "$FAKE_SHA" ;;
  archive) cat "$FAKE_STATE/source.tar" ;;
  *) ;;
esac
"""

FAKE_DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$FAKE_STATE/calls.log"
verb=""
for a in "$@"; do case "$a" in config) verb=config ;; build) verb=build ;; up) verb=up ;; esac; done
case "$1" in
  inspect) echo "sha256:previousimage" ;;
  image)
    case "$2" in
      tag) ;;
      ls) [ "$FAKE_IMAGE_LS_FAIL" = 1 ] && { echo "daemon down" >&2; exit 1; }; cat "$FAKE_STATE/ids.txt" ;;
      inspect) cat "$FAKE_STATE/images.json" ;;
    esac ;;
  compose)
    case "$verb" in
      config) [ "$FAKE_CONFIG_FAIL" = 1 ] && { echo "variable is not set" >&2; exit 1; }; cat "$FAKE_STATE/config.json" ;;
      build) [ "$FAKE_BUILD_FAIL" = 1 ] && { echo "build failed" >&2; exit 1; }; ;;
      up) ;;
    esac ;;
  exec) ;;
esac
exit 0
"""

FAKE_SHA256SUM = r"""#!/usr/bin/env python3
import hashlib, sys
for path in sys.argv[1:]:
    print(hashlib.sha256(open(path, "rb").read()).hexdigest() + "  " + path)
"""


class Server:
    """A temporary HOME with the repo checkout, the fake git/docker and the files they answer with."""

    def __init__(self, test):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.home, self.bin, self.state = root / "home", root / "bin", root / "state"
        for d in (self.home / "algo" / SRCDIR / ".github" / "scripts", self.bin, self.state):
            d.mkdir(parents=True)
        (self.home / "algo" / SRCDIR / ".github" / "scripts" / "release_image.py").symlink_to(HERE / "release_image.py")
        for name, text in (("git", FAKE_GIT), ("docker", FAKE_DOCKER), ("sha256sum", FAKE_SHA256SUM)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        with tarfile.open(self.state / "source.tar", "w") as archive:
            data = root / "hello.txt"
            data.write_text("source")
            archive.add(data, arcname="hello.txt")
        self.archive_sha = hashlib.sha256((self.state / "source.tar").read_bytes()).hexdigest()
        self.build_section = {"context": "../../x", "dockerfile": "Dockerfile", "target": "production"}
        self.write_config()
        self.write_images([])

    def write_config(self, **build):
        section = dict(self.build_section, **build)
        (self.state / "config.json").write_text(json.dumps({"services": {SERVICE: {"build": section}}}))
        return section

    def fingerprint(self, **build):
        sys.path.insert(0, str(HERE))
        import release_image
        return release_image.fingerprint({"services": {SERVICE: {"build": dict(self.build_section, **build)}}}, SERVICE)

    def write_images(self, images):
        (self.state / "ids.txt").write_text("\n".join(i["Id"] for i in images) + ("\n" if images else ""))
        (self.state / "images.json").write_text(json.dumps(images))

    def image(self, iid="sha256:stageimage", sha=SHA, archive=None, fp=None, service=SERVICE):
        labels = {"org.opencontainers.image.revision": sha, "io.algolab.source.archive.sha256": archive or self.archive_sha,
                  "io.algolab.build.fingerprint": fp or self.fingerprint(), SERVICE_MARK: service}
        return {"Id": iid, "Created": "2026-11-01T10:00:00Z", "Config": {"Labels": labels}}

    def run(self, **env):
        base = dict(os.environ, HOME=str(self.home), PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state), FAKE_SHA=SHA,
                    FAKE_DIRTY="", FAKE_IMAGE_LS_FAIL="0", FAKE_CONFIG_FAIL="0", FAKE_BUILD_FAIL="0")
        base.update(env)
        args = [SRCDIR, "algo-infrastructure/compose/docker-compose.staging.yml", PROJECT, ".env.staging", "algo-staging-svc", SHA, "main", "1-1",
                "2026-11-01T10:00:00Z", SERVICE]
        (self.home / "algo" / ".env.staging").write_text("")
        return subprocess.run(["bash", "-s", "--", *args], input=step_body(), text=True, capture_output=True, env=base, timeout=60)

    def calls(self):
        return (self.state / "calls.log").read_text().splitlines() if (self.state / "calls.log").exists() else []

    def release_dir(self):
        (path,) = (self.home / "algo" / "releases").iterdir()
        return path

    def record(self):
        return json.loads((self.release_dir() / "release-record.json").read_text())

    def override(self):
        return json.loads((self.release_dir() / "source.compose.json").read_text())["services"][SERVICE]["build"]


def compose_calls(calls, verb):
    return [c for c in calls if c.startswith("docker compose ") and re.search(rf" {verb}( |$)", c)]


class ReuseTests(unittest.TestCase):
    def test_a_matching_stage_image_is_tagged_and_started_without_a_build(self):
        s = Server(self)
        s.write_images([s.image()])
        done = s.run()
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = s.calls()
        self.assertEqual(compose_calls(calls, "build"), [])
        (up,) = compose_calls(calls, "up")
        self.assertIn("--no-build", up)
        self.assertIn("reuse.compose.json", up)
        tag = f"algolab-reuse-{PROJECT}-{SERVICE}:{SHA[:12]}-{s.fingerprint()[:12]}"
        self.assertIn(f"docker image tag sha256:stageimage {tag}", calls)
        reuse = json.loads((s.release_dir() / "reuse.compose.json").read_text())
        self.assertEqual(reuse, {"services": {SERVICE: {"image": tag}}})
        self.assertEqual((s.record()["image_source"], s.record()["reused_image_id"]), ("reused", "sha256:stageimage"))
        self.assertIn("Образ переиспользован: sha256:stageimage", done.stdout)
        self.assertIn(SHA, done.stdout)

    def test_no_image_on_the_server_builds_as_before(self):
        s = Server(self)
        done = s.run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(compose_calls(s.calls(), "build")), 1)
        (up,) = compose_calls(s.calls(), "up")
        self.assertNotIn("reuse.compose.json", up)
        self.assertEqual(s.record()["image_source"], "built")
        self.assertIn("собираю", done.stdout)

    def assert_builds(self, s, **env):
        done = s.run(**env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(compose_calls(s.calls(), "build")), 1, s.calls())
        self.assertFalse([c for c in s.calls() if c.startswith("docker image tag sha256:stageimage")])
        self.assertNotIn("reuse.compose.json", compose_calls(s.calls(), "up")[0])

    def test_an_image_of_another_revision_is_built_over(self):
        s = Server(self)
        s.write_images([s.image(sha="b" * 40)])
        self.assert_builds(s)

    def test_another_build_configuration_is_built_over(self):
        s = Server(self)
        s.write_images([s.image(fp=s.fingerprint(args={"NEXT_PUBLIC_AUTH_API_URL": "https://stage.example"}))])
        self.assert_builds(s)

    def test_another_source_archive_is_built_over(self):
        s = Server(self)
        s.write_images([s.image(archive="d" * 64)])
        self.assert_builds(s)

    def test_another_service_is_built_over(self):
        s = Server(self)
        s.write_images([s.image(service="other")])
        self.assert_builds(s)

    def test_docker_failing_to_list_images_builds(self):
        s = Server(self)
        s.write_images([s.image()])
        self.assert_builds(s, FAKE_IMAGE_LS_FAIL="1")

    def test_a_config_that_cannot_be_read_builds_and_labels_nothing_false(self):
        s = Server(self)
        s.write_images([s.image()])
        self.assert_builds(s, FAKE_CONFIG_FAIL="1")
        self.assertEqual(s.override()["labels"]["io.algolab.build.fingerprint"], "")

    def test_the_build_labels_carry_the_identity_a_later_reuse_needs(self):
        s = Server(self)
        self.assertEqual(s.run().returncode, 0)
        labels = s.override()["labels"]
        self.assertEqual(labels["org.opencontainers.image.revision"], SHA)
        self.assertEqual(labels["io.algolab.source.archive.sha256"], s.archive_sha)
        self.assertEqual(labels["io.algolab.build.fingerprint"], s.fingerprint())
        self.assertEqual(labels[SERVICE_MARK], SERVICE)
        self.assertEqual(s.record()["build_fingerprint"], s.fingerprint())

    def test_a_failed_build_deploys_nothing(self):
        s = Server(self)
        done = s.run(FAKE_BUILD_FAIL="1")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(compose_calls(s.calls(), "up"), [])
        self.assertFalse([c for c in s.calls() if "nginx" in c])

    def test_the_old_guards_of_the_step_are_still_there(self):
        s = Server(self)
        dirty = s.run(FAKE_DIRTY="1")
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("modified server checkout", dirty.stderr)
        self.assertEqual(compose_calls(s.calls(), "up"), [])
        s2 = Server(self)
        wrong = s2.run(FAKE_SHA="c" * 40)                  # the checkout is not at the tested revision
        self.assertNotEqual(wrong.returncode, 0)
        self.assertEqual(compose_calls(s2.calls(), "up"), [])
        s3 = Server(self)
        s3.write_images([s3.image()])
        self.assertEqual(s3.run().returncode, 0)
        self.assertIn("docker image tag sha256:previousimage algolab-rollback-" + PROJECT + "-" + SERVICE + ":" + SHA[:12] + "-1-1", s3.calls())
        self.assertIn("docker exec algo-staging-nginx nginx -t", s3.calls())

    def test_the_up_always_runs_without_a_build_and_without_dependencies(self):
        for images in ([], "match"):
            s = Server(self)
            if images:
                s.write_images([s.image()])
            self.assertEqual(s.run().returncode, 0)
            (up,) = compose_calls(s.calls(), "up")
            self.assertIn("up -d --no-build --no-deps " + SERVICE, up)


if __name__ == "__main__":
    unittest.main()
