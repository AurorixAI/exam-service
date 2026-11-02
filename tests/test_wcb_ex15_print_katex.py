"""WC-B EX-15: one bad formula, one hung browser or a slow document cannot cost the whole print job.

The old behaviour (measured against the code of origin/main with the same fake node/Chromium, see the numbers printed by the tests):
  * node threw on ONE refused formula -> the whole batch of 32 and every batch rendered before it were thrown away (``except Exception: return {}``);
    each formula then went alone through node + Chromium again (1.5-2 s each in production) - 30 formulas = 30 more browser launches;
  * a Chromium that hangs cost 20 s per formula, 30 formulas = 10+ minutes in one of the three heavy slots, nginx cut the tab at 120 s.
Now: node marks the refused formulas (``<!--failed:i,j-->``, their rows stay empty), the others are drawn from the same page; a failed or timed
out batch costs only its formulas; formulas KaTeX refused are never sent to KaTeX again; the document has ONE budget (60 s) of node/Chromium time,
after it every remaining formula goes straight to the fallback renderer.  ``throwOnError`` stays true (a red TeX source in the paper is worse).
``subprocess.run`` is replaced (pattern of test_wa4_public_surfaces), the clock is a fake one: no real browser, no real time."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import types
from io import BytesIO
from pathlib import Path

import pytest

from src.domain import print_pdf

ROOT = Path(print_pdf.__file__).resolve().parents[2]
GOOD = (r"\frac{1}{2}", 12.0, "#101B35")


def formulas(n, bad=()):
    return [(rf"x^{{{i}}}" if i not in bad else rf"\bad{{{i}", 12.0, "#101B35") for i in range(n)]


class Fake:
    """Fake node + Chromium.  ``refuse``: latex strings the (fake) KaTeX throws on.  ``clock``: advanced by the time each call «takes»."""

    def __init__(self, monkeypatch, *, refuse=(), chromium_seconds=0.0, chromium_fail_batches=(), chromium_hang_batches=(), node_fail_batches=(), legacy_node=False):
        self.refuse, self.legacy_node = set(refuse), legacy_node
        self.chromium_seconds, self.fail, self.hang, self.node_fail = chromium_seconds, set(chromium_fail_batches), set(chromium_hang_batches), set(node_fail_batches)
        self.calls = []                  # ("node"|"chromium", number of formulas, timeout)
        self.now = 1000.0
        self.node_batches = 0
        self.chromium_batches = 0
        monkeypatch.setattr(print_pdf.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(print_pdf.Path, "is_file", lambda self: True)
        monkeypatch.setattr(print_pdf.subprocess, "run", self)
        monkeypatch.setattr(print_pdf, "time", types.SimpleNamespace(monotonic=lambda: self.now, sleep=lambda s: None), raising=False)
        monkeypatch.setattr(print_pdf, "_KATEX_BROKEN", set(), raising=False)
        monkeypatch.setattr(print_pdf, "_MATH_ARTIFACT_CACHE", {})

    def __call__(self, cmd, **kw):
        if str(cmd[0]).endswith("node"):
            payload = json.loads(kw["input"])
            items = payload["formulas"]
            batch_no, self.node_batches = self.node_batches, self.node_batches + 1
            self.calls.append(("node", len(items), kw.get("timeout")))
            if batch_no in self.node_fail:
                raise subprocess.CalledProcessError(1, cmd)
            refused = [i for i, item in enumerate(items) if item["latex"] in self.refuse]
            if refused and self.legacy_node:
                raise subprocess.CalledProcessError(1, cmd)          # the old script: throwOnError threw out of the whole batch
            return types.SimpleNamespace(stdout=f"<!doctype html><head><!--failed:{','.join(map(str, refused))}--></head>")
        batch_no, self.chromium_batches = self.chromium_batches, self.chromium_batches + 1
        shot = next(a for a in cmd if str(a).startswith("--screenshot=")).split("=", 1)[1]
        rows = int(re.search(r"--window-size=1600,(\d+)", " ".join(map(str, cmd))).group(1)) // print_pdf._KATEX_BATCH_ROW_HEIGHT
        self.calls.append(("chromium", rows, kw.get("timeout")))
        self.now += self.chromium_seconds
        if batch_no in self.hang:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        if batch_no in self.fail:
            raise subprocess.CalledProcessError(1, cmd)
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", (1600, print_pdf._KATEX_BATCH_ROW_HEIGHT * rows), (255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        for r in range(rows):
            top = r * print_pdf._KATEX_BATCH_ROW_HEIGHT + 40
            draw.rectangle((20, top, 140, top + 30), fill=(16, 27, 53, 255))
        image.save(shot)
        return types.SimpleNamespace(stdout=b"")

    def count(self, program):
        return sum(1 for c in self.calls if c[0] == program)


def test_one_refused_formula_costs_only_itself(monkeypatch):
    reqs = formulas(30, bad={7})
    fake = Fake(monkeypatch, refuse={reqs[7][0]})
    done = print_pdf._render_katex_artifacts(reqs)
    assert set(done) == set(reqs) - {reqs[7]}                       # 29 drawn, the refused one left for the fallback
    assert fake.count("node") == 1 and fake.count("chromium") == 1
    assert reqs[7][0] in print_pdf._KATEX_BROKEN
    print(f"\nEX-15 30 formulas, 1 refused: node+chromium calls {fake.count('node')}+{fake.count('chromium')}, drawn {len(done)}/30")


def test_the_old_script_loses_the_whole_batch(monkeypatch):
    """The same input with a script that throws on the refused formula (the code of origin/main): everything is lost, then 29 single launches."""
    reqs = formulas(30, bad={7})
    fake = Fake(monkeypatch, refuse={reqs[7][0]}, legacy_node=True)
    done = print_pdf._render_katex_artifacts(reqs)
    assert done == {} and fake.count("chromium") == 0
    # what the paper path did next (origin/main): every formula alone
    singles = 0
    for r in reqs:
        singles += 1
        print_pdf._render_katex_artifacts([r])
    print(f"EX-15 origin/main behaviour: drawn 0/30 from the batch, then {singles} single node launches (+ a Chromium each for the 29 good ones in production)")
    assert singles == 30


def test_a_refused_formula_is_never_sent_to_katex_again(monkeypatch):
    reqs = formulas(3, bad={1})
    fake = Fake(monkeypatch, refuse={reqs[1][0]})
    print_pdf._render_katex_artifacts(reqs)
    calls = len(fake.calls)
    assert print_pdf._render_katex_artifact(*reqs[1]) is None
    assert len(fake.calls) == calls                                   # no node, no Chromium for it
    assert print_pdf._render_katex_artifact(*reqs[0]) is not None     # a good one still works alone
    assert print_pdf._MATH_ARTIFACT_CACHE == {}                        # nothing negative cached: the matplotlib path is not skipped


def test_a_failed_batch_keeps_the_ones_before_it_and_the_next_is_tried(monkeypatch):
    reqs = formulas(70)                                                # batches of 32, 32, 6
    fake = Fake(monkeypatch, chromium_fail_batches={1})
    done = print_pdf._render_katex_artifacts(reqs)
    assert set(done) == set(reqs[:32]) | set(reqs[64:])               # batch 2 lost, batch 1 kept, batch 3 drawn
    assert fake.count("chromium") == 3


def test_a_node_failure_of_a_batch_costs_that_batch_only(monkeypatch):
    reqs = formulas(70)
    fake = Fake(monkeypatch, node_fail_batches={0})
    done = print_pdf._render_katex_artifacts(reqs)
    assert set(done) == set(reqs[32:])


def test_a_hung_browser_stops_the_document_and_keeps_what_was_drawn(monkeypatch):
    reqs = formulas(70)
    fake = Fake(monkeypatch, chromium_hang_batches={1}, chromium_seconds=20.0)
    token = print_pdf._katex_deadline.set(fake.now + 60)
    try:
        done = print_pdf._render_katex_artifacts(reqs)
        assert set(done) == set(reqs[:32])                             # the batch before the hang is kept
        assert fake.count("chromium") == 2                             # the third batch is not even started
        assert all(print_pdf._render_katex_artifact(*r) is None for r in reqs[32:40])      # the rest of the document: fallback at once
        assert fake.count("chromium") == 2
    finally:
        print_pdf._katex_deadline.reset(token)


def test_a_hung_browser_without_a_document_budget_does_not_poison_the_thread(monkeypatch):
    """A direct caller (a preview) has no deadline: a timeout stops that call, and does not leave a spent budget behind for the thread."""
    fake = Fake(monkeypatch, chromium_hang_batches={0})
    assert print_pdf._katex_deadline.get() is None
    print_pdf._render_katex_artifacts(formulas(5))
    assert print_pdf._katex_deadline.get() is None


def test_the_document_budget_limits_the_browser_time(monkeypatch):
    """Slow (not hung) Chromium: 25 s per batch.  Budget 60 s -> 3 batches at most, then the fallback; the timeouts handed to the processes shrink."""
    reqs = formulas(32 * 6)
    fake = Fake(monkeypatch, chromium_seconds=25.0)
    token = print_pdf._katex_deadline.set(fake.now + print_pdf.KATEX_DOCUMENT_BUDGET_S)
    try:
        done = print_pdf._render_katex_artifacts(reqs)
    finally:
        print_pdf._katex_deadline.reset(token)
    assert fake.count("chromium") == 3 and len(done) == 96
    assert [c[2] for c in fake.calls if c[0] == "chromium"] == [20, 20, 10]          # the last launch gets only what is left of the 60 s
    print(f"EX-15 slow browser (25 s a batch), 192 formulas, budget 60 s: Chromium launches {fake.count('chromium')} (bound 3), drawn {len(done)}")


def test_the_whole_paper_is_still_printed_with_a_refused_formula(monkeypatch):
    """The PDF of a variant with 3 formulas, one of them refused by KaTeX: a PDF comes out and the refused formula IS drawn (matplotlib), not dropped."""
    from tests.test_print_pdf import _print_snapshot

    snapshot = _print_snapshot()
    task = snapshot["variants"][0]["tasks"][0]
    task["question_latex"] = "$" + r"\frac{1}{2}" + "$ и $" + r"\sqrt{3}" + "$ и $" + r"\notaknowncommand{7" + "$"
    fake = Fake(monkeypatch, refuse={r"\sqrt{3}", r"\notaknowncommand{7"})
    pdf = print_pdf.render_student_exam_pdf(snapshot, 1)
    assert pdf.startswith(b"%PDF")
    assert r"\notaknowncommand{7" in print_pdf._KATEX_BROKEN
    assert r"\sqrt{3}" in print_pdf._KATEX_BROKEN                                    # remembered: not sent to KaTeX a second time
    assert fake.count("node") == 1 and fake.count("chromium") == 1                   # ONE page for the whole variant, no single launches
    # R3 M1: the refused formula really has a picture (the readable fallback), and so has the good one
    node_calls = fake.count("node")
    drawn = print_pdf._render_math_artifact(r"\sqrt{3}", 12.0, "#101B35")
    assert drawn is not None and drawn.png.startswith(b"\x89PNG") and drawn.width > 0 and drawn.height > 0
    assert fake.count("node") == node_calls                                          # the refused formula did not go back to KaTeX


# ── R3 B2: a passing refusal of KaTeX is not remembered, a lasting one is ──────────────────────────────────────────────────
def test_a_timeout_is_not_remembered_the_next_print_asks_katex_again(monkeypatch):
    latex = r"\frac{3}{7}"
    fake = Fake(monkeypatch, chromium_hang_batches={0})
    first = print_pdf._render_math_artifact(latex, 12.0, "#101B35")
    assert first is not None                                             # the matplotlib picture: the paper is never empty
    assert fake.count("chromium") == 1 and len(print_pdf._MATH_ARTIFACT_CACHE) == 0     # nothing remembered
    healed = print_pdf._render_math_artifact(latex, 12.0, "#101B35")   # the browser is healthy now
    assert fake.count("chromium") == 2                                   # KaTeX was asked again
    assert healed is not first and len(print_pdf._MATH_ARTIFACT_CACHE) == 1      # and its (fake KaTeX) picture is what is remembered now
    n = len(fake.calls)
    assert print_pdf._render_math_artifact(latex, 12.0, "#101B35") is healed and len(fake.calls) == n


def test_a_spent_document_budget_is_not_remembered_either(monkeypatch):
    latex = r"\frac{5}{9}"
    fake = Fake(monkeypatch)
    token = print_pdf._katex_deadline.set(fake.now - 1)                  # the document's 60 s are gone
    try:
        assert print_pdf._render_math_artifact(latex, 12.0, "#101B35") is not None
    finally:
        print_pdf._katex_deadline.reset(token)
    assert len(print_pdf._MATH_ARTIFACT_CACHE) == 0 and fake.count("chromium") == 0
    assert print_pdf._render_math_artifact(latex, 12.0, "#101B35") is not None        # next print, a fresh budget
    assert fake.count("chromium") == 1


def test_a_formula_katex_refused_is_remembered_with_its_fallback(monkeypatch):
    latex = r"\frac{2}{11}"
    fake = Fake(monkeypatch, refuse={latex})
    first = print_pdf._render_math_artifact(latex, 12.0, "#101B35")
    assert first is not None
    n = len(fake.calls)
    assert print_pdf._render_math_artifact(latex, 12.0, "#101B35") is first and len(fake.calls) == n      # a lasting reason: cached


def test_without_katex_installed_the_fallback_is_remembered(monkeypatch):
    """No node/Chromium on this machine is not a passing failure: every print would otherwise redraw every formula with matplotlib."""
    monkeypatch.setattr(print_pdf.shutil, "which", lambda name: None)
    monkeypatch.setattr(print_pdf, "_MATH_ARTIFACT_CACHE", print_pdf._BoundedCache(10))
    latex = r"\frac{4}{13}"
    first = print_pdf._render_math_artifact(latex, 12.0, "#101B35")
    assert first is not None and print_pdf._render_math_artifact(latex, 12.0, "#101B35") is first


def test_the_broken_set_is_bounded(monkeypatch):
    broken = print_pdf._BoundedCache(2000)
    for i in range(2500):
        broken.add(f"x^{{{i}")
    assert len(broken) == 2000 and "x^{0" not in broken and "x^{2499" in broken
    assert isinstance(print_pdf._KATEX_BROKEN, print_pdf._BoundedCache)


@pytest.mark.skipif(not (shutil.which("node") and (ROOT / "node_modules" / "katex").is_dir()), reason="needs node and `npm install` (katex)")
def test_the_real_script_marks_the_refused_formula_and_exits_zero():
    script = ROOT / "scripts" / "render_katex_html.cjs"
    payload = json.dumps({"formulas": [
        {"latex": "x^2", "fontSize": 20, "color": "#000"},
        {"latex": r"\frac{1}{", "fontSize": 20, "color": "#000"},
        {"latex": r"\sqrt{2}", "fontSize": 20, "color": "#000"},
    ], "rowHeight": 112})
    out = subprocess.run(["node", str(script)], input=payload, text=True, capture_output=True,
                         env={"KATEX_CSS_PATH": str(ROOT / "node_modules/katex/dist/katex.min.css"), "PATH": os.environ["PATH"]})
    assert out.returncode == 0, out.stderr
    assert print_pdf._FAILED_MARKER.search(out.stdout).group(1) == "1"
    assert out.stdout.count('class="formula-row"') == 3                       # the empty row keeps the rows below in place
    assert "katex-error" not in out.stdout                                    # throwOnError stayed true: no red TeX source on the paper
