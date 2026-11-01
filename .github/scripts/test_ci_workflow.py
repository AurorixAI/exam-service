#!/usr/bin/env python3
"""Static guards of the CI speed pack (#140): the pieces of deploy.yml that make «skip the tests» and «reuse the image» safe stay wired together.
Text checks only (no YAML library on the runner)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
TEST_JOB = "test"                      # the job that runs the suite
SHARDS = 0                             # 0 = a single job without a matrix


def job(name):
    """The text of one top-level job."""
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", WORKFLOW, re.S | re.M)
    assert m, f"job {name} not found"
    return m.group(1)


def run_lines(text):
    """Lines of the shell blocks (``run: |``) only."""
    out, inside, indent = [], False, 0
    for line in text.splitlines():
        stripped = line.strip()
        if inside:
            if stripped and len(line) - len(line.lstrip()) <= indent:
                inside = False
            else:
                out.append(line)
                continue
        m = re.match(r"^(\s*)(- )?run: [|>]", line)
        if m:
            inside, indent = True, len(m.group(1)) + (2 if m.group(2) else 0)
        elif re.match(r"^\s*(- )?run: ", line):
            out.append(line)
    return out


class GateWiring(unittest.TestCase):
    def test_the_gate_reads_the_runs_and_nothing_more(self):
        gate = job("gate")
        self.assertRegex(gate, r"actions: read")
        self.assertRegex(gate, r"contents: read")
        self.assertIn("ci_gate.py gate", gate)
        self.assertIn("--ref-name \"${{ github.ref_name }}\"", gate)
        self.assertIn("--sha \"${{ github.sha }}\"", gate)
        self.assertRegex(gate, r"outputs:\n\s+skip_tests: \$\{\{ steps\.g\.outputs\.skip_tests \}\}")

    def test_the_gate_expects_as_many_test_jobs_as_the_workflow_has(self):
        expected = SHARDS or 1
        self.assertIn(f"--expect-tests {expected}", job("gate"))
        if SHARDS:
            matrix = re.search(r"shard: \[([0-9, ]+)\]", job(TEST_JOB)).group(1)
            self.assertEqual([int(x) for x in matrix.split(",")], list(range(1, SHARDS + 1)))
            self.assertIn(f"/{SHARDS})", job(TEST_JOB))                                   # the job name
            self.assertIn(f"ci_shard.py --check {SHARDS}", job(TEST_JOB))
            self.assertIn(f"ci_shard.py ${{{{ matrix.shard }}}} {SHARDS}", job(TEST_JOB))

    def test_the_tests_are_skipped_only_on_the_word_of_the_gate_and_run_when_it_is_silent(self):
        text = job(TEST_JOB)
        self.assertRegex(text, r"needs: gate")
        condition = re.search(r"^    if: (.*)$", text, re.M).group(1)
        self.assertIn("needs.gate.outputs.skip_tests != 'true'", condition)
        self.assertIn("!cancelled()", condition)                                           # a failed gate still lets them run

    def test_the_deploy_waits_for_the_verdict_which_knows_the_gate(self):
        ok = job("tests-ok")
        self.assertRegex(ok, rf"needs: \[gate, guards, {TEST_JOB}\]")
        self.assertRegex(ok, r"if: \$\{\{ always\(\) \}\}")
        self.assertIn("ci_gate.py verdict", ok)
        for part in ("needs.gate.result", "needs.gate.outputs.skip_tests", f"needs.{TEST_JOB}.result", "needs.guards.result"):
            self.assertIn(part, ok)
        self.assertRegex(job("deploy"), r"needs: tests-ok")

    def test_the_deploy_guard_tests_run_in_their_own_job_whatever_the_gate_says(self):
        guards = job("guards")
        self.assertNotIn("needs:", guards)
        self.assertNotIn("if:", guards)
        self.assertIn("test_*.py", guards)

    def test_text_from_the_api_never_reaches_a_shell_line(self):
        """The reason holds branch names and states from the API: it goes through env, never into a script."""
        for name in ("gate", "tests-ok", "deploy"):
            for line in run_lines(job(name)):
                self.assertNotIn("gate.outputs.reason", line)

    def test_the_deploy_still_runs_only_for_main_and_develop(self):
        self.assertRegex(job("deploy"), r"if: github\.ref_name == 'main' \|\| github\.ref_name == 'develop'")


class ReuseWiring(unittest.TestCase):
    def test_the_build_step_decides_between_reuse_and_build_before_up(self):
        deploy = job("deploy")
        find = deploy.index('"$release_tool" find')
        build = deploy.index('build "$service"')
        up = deploy.index("up -d --no-build --no-deps")
        self.assertLess(find, build)
        self.assertLess(build, up)
        self.assertIn('--archive "$archive_sha"', deploy)
        self.assertIn('--sha "$sha"', deploy)

    def test_the_labels_that_a_later_reuse_needs_are_written_at_every_build(self):
        for label in ("org.opencontainers.image.revision", "io.algolab.source.archive.sha256", "io.algolab.build.fingerprint", "io.algolab.service"):
            self.assertIn(f'"{label}"', job("deploy"))

    def test_the_guards_after_the_start_are_untouched(self):
        deploy = job("deploy")
        for step in ("Verify container is healthy", "Verify deployed revision", "Export deployed release manifest",
                     "Include source archive and rollback identity", "Verify site responds"):
            self.assertIn(f"- name: {step}", deploy)
        self.assertIn("rollback_tag=", deploy)
        self.assertIn("git merge-base --is-ancestor", deploy)
        self.assertIn("Refusing deployment from a modified server checkout", deploy)


if __name__ == "__main__":
    unittest.main()
