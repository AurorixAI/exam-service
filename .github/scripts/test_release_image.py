#!/usr/bin/env python3
"""«Reuse the stage image on production» (release_image.py): when the image is taken and, above all, when it is NOT."""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_image as ri  # noqa: E402

SHA = "a" * 40
ARCHIVE = "c" * 64
SERVICE = "auth-service"


def config(context="/srv/preview-src/auth", labels=None, **build):
    section = {"context": context, "dockerfile": "Dockerfile", **build}
    if labels is not None:
        section["labels"] = labels
    return {"name": "algo-preview", "services": {SERVICE: {"build": section, "container_name": "x"}, "other": {"build": {"context": "/o"}}}}


def image(iid, sha=SHA, fp=None, archive=ARCHIVE, service=SERVICE, created="2026-11-01T10:00:00Z", labels=True):
    fp = fp or ri.fingerprint(config(target="production"), SERVICE)
    meta = {ri.REVISION: sha, ri.FINGERPRINT: fp, ri.ARCHIVE: archive, ri.SERVICE: service} if labels else None
    return {"Id": iid, "Created": created, "Config": {"Labels": meta}}


class FingerprintTests(unittest.TestCase):
    def test_context_and_labels_do_not_change_it(self):
        stage = ri.fingerprint(config("/srv/preview-src/auth", {"a": "1"}, target="production"), SERVICE)
        prod = ri.fingerprint(config("/srv/auth", {"io.algolab.source.archive.sha256": "x"}, target="production"), SERVICE)
        self.assertEqual(stage, prod)
        self.assertRegex(stage, r"^[0-9a-f]{64}$")

    def test_a_different_build_argument_changes_it(self):
        stage = ri.fingerprint(config(args={"NEXT_PUBLIC_AUTH_API_URL": ""}), SERVICE)
        same = ri.fingerprint(config(args={"NEXT_PUBLIC_AUTH_API_URL": ""}), SERVICE)
        prod = ri.fingerprint(config(args={"NEXT_PUBLIC_AUTH_API_URL": "https://app.algolab.me"}), SERVICE)
        self.assertEqual(stage, same)
        self.assertNotEqual(stage, prod)

    def test_a_new_or_removed_argument_changes_it(self):
        base = ri.fingerprint(config(args={"A": ""}), SERVICE)
        self.assertNotEqual(base, ri.fingerprint(config(args={"A": "", "B": ""}), SERVICE))
        self.assertNotEqual(base, ri.fingerprint(config(), SERVICE))

    def test_target_and_dockerfile_change_it(self):
        base = ri.fingerprint(config(target="production"), SERVICE)
        self.assertNotEqual(base, ri.fingerprint(config(target="builder"), SERVICE))
        self.assertNotEqual(base, ri.fingerprint(config(target="production", dockerfile="Dockerfile.prod"), SERVICE))

    def test_the_order_of_arguments_does_not_matter(self):
        self.assertEqual(ri.fingerprint(config(args={"A": "1", "B": "2"}), SERVICE), ri.fingerprint(config(args={"B": "2", "A": "1"}), SERVICE))

    def test_the_short_form_of_build_works(self):
        short = {"services": {SERVICE: {"build": "./dir"}}}
        self.assertRegex(ri.fingerprint(short, SERVICE), r"^[0-9a-f]{64}$")

    def test_a_service_without_build_is_an_error_not_a_hash(self):
        with self.assertRaises(KeyError):
            ri.fingerprint({"services": {SERVICE: {"image": "x"}}}, SERVICE)

    def test_the_command_reads_the_compose_json(self):
        text = json.dumps(config(target="production"))
        done = subprocess.run([sys.executable, str(Path(ri.__file__)), "fingerprint", "--service", SERVICE], input=text, capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ri.fingerprint(config(target="production"), SERVICE)))
        bad = subprocess.run([sys.executable, str(Path(ri.__file__)), "fingerprint", "--service", "nope"], input=text, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)


class PickTests(unittest.TestCase):
    FP = ri.fingerprint(config(target="production"), SERVICE)

    def pick(self, images, **over):
        want = dict(sha=SHA, fp=self.FP, archive=ARCHIVE, service=SERVICE)
        want.update(over)
        return ri.pick(images, want["sha"], want["fp"], want["archive"], want["service"])

    def test_an_image_with_the_same_revision_archive_fingerprint_and_service_is_taken(self):
        found, reason = self.pick([image("sha256:one")])
        self.assertEqual(found, "sha256:one")
        self.assertIn("match", reason)

    def test_the_newest_of_several_is_taken(self):
        self.assertEqual(self.pick([image("sha256:old", created="2026-11-01T09:00:00Z"), image("sha256:new", created="2026-11-01T11:00:00Z")])[0], "sha256:new")

    def test_an_image_of_another_revision_is_built_over(self):
        found, reason = self.pick([image("sha256:one", sha="b" * 40)])
        self.assertIsNone(found)
        self.assertIn("no image of this revision", reason)

    def test_the_same_revision_but_another_build_configuration_is_built_over(self):
        other = ri.fingerprint(config(args={"NEXT_PUBLIC_AUTH_API_URL": "https://x"}), SERVICE)
        found, reason = self.pick([image("sha256:one", fp=other)])
        self.assertIsNone(found)
        self.assertIn(ri.FINGERPRINT, reason)

    def test_the_same_revision_but_another_source_archive_is_built_over(self):
        found, reason = self.pick([image("sha256:one", archive="d" * 64)])
        self.assertIsNone(found)
        self.assertIn(ri.ARCHIVE, reason)

    def test_the_same_revision_but_another_service_is_built_over(self):
        self.assertIsNone(self.pick([image("sha256:one", service="exam-service")])[0])

    def test_an_image_without_our_labels_is_never_taken(self):
        self.assertIsNone(self.pick([image("sha256:one", labels=False)])[0])
        self.assertIsNone(self.pick([{"Id": "sha256:two", "Config": {}}, {"Id": "sha256:three"}])[0])

    def test_no_images_or_an_empty_identity_means_a_build(self):
        self.assertIsNone(self.pick([])[0])
        for key in ("sha", "fp", "archive", "service"):
            self.assertIsNone(self.pick([image("sha256:one")], **{key: ""})[0], key)

    def test_a_match_is_found_among_wrong_ones(self):
        images = [image("sha256:a", sha="b" * 40), image("sha256:b", archive="d" * 64), image("sha256:c")]
        self.assertEqual(self.pick(images)[0], "sha256:c")


FAKE_DOCKER = """#!/bin/sh
# image ls -q ... -> the ids in $FAKE_IDS; image inspect ... -> $FAKE_INSPECT (a json file); FAKE_FAIL=1 -> docker is broken
[ "$FAKE_FAIL" = 1 ] && { echo "Cannot connect to the Docker daemon" >&2; exit 1; }
case "$1 $2" in
  "image ls") printf '%s\\n' $FAKE_IDS ;;
  "image inspect") cat "$FAKE_INSPECT" ;;
  *) exit 88 ;;
esac
"""


class FindCommandTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.bin = Path(self.dir.name) / "docker"
        self.bin.write_text(FAKE_DOCKER)
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IEXEC)
        self.fp = PickTests.FP

    def tearDown(self):
        self.dir.cleanup()

    def find(self, images, ids=None, fail=False):
        inspect = Path(self.dir.name) / "inspect.json"
        inspect.write_text(json.dumps(images))
        env = dict(os.environ, RELEASE_IMAGE_DOCKER=str(self.bin), FAKE_IDS=" ".join(ids if ids is not None else [i["Id"] for i in images]),
                   FAKE_INSPECT=str(inspect), FAKE_FAIL="1" if fail else "0")
        return subprocess.run([sys.executable, str(Path(ri.__file__)), "find", "--sha", SHA, "--fingerprint", self.fp, "--archive", ARCHIVE,
                               "--service", SERVICE], capture_output=True, text=True, env=env)

    def test_the_id_is_printed_for_a_match(self):
        done = self.find([image("sha256:one")])
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "sha256:one"))
        self.assertIn("match", done.stderr)

    def test_nothing_is_printed_for_other_revision_configuration_or_archive(self):
        for wrong in (image("sha256:one", sha="b" * 40), image("sha256:one", fp="e" * 64), image("sha256:one", archive="d" * 64)):
            done = self.find([wrong])
            self.assertEqual((done.returncode, done.stdout.strip()), (0, ""), wrong)

    def test_a_broken_docker_or_an_empty_list_means_a_build_not_an_error(self):
        done = self.find([image("sha256:one")], fail=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ""))
        self.assertIn("did not answer clearly", done.stderr)
        done = self.find([], ids=[])
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ""))

    def test_a_missing_docker_binary_means_a_build(self):
        env = dict(os.environ, RELEASE_IMAGE_DOCKER="/nonexistent/docker")
        done = subprocess.run([sys.executable, str(Path(ri.__file__)), "find", "--sha", SHA, "--fingerprint", self.fp, "--archive", ARCHIVE,
                               "--service", SERVICE], capture_output=True, text=True, env=env)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ""))


class NoteTests(unittest.TestCase):
    def test_keys_are_added_to_the_record(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "release-record.json"
            path.write_text(json.dumps({"service": "auth-service"}))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                ri.main(["note", "--file", str(path), "image_source=reused", "image_id=sha256:abc"])
            self.assertEqual(json.loads(path.read_text()), {"service": "auth-service", "image_source": "reused", "image_id": "sha256:abc"})


if __name__ == "__main__":
    unittest.main()
