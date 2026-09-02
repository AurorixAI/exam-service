"""Deterministic PDF renderer for the offline exam delivery mode.

The renderer accepts only a prepared :class:`ExamPrintPackage` snapshot.  It
never reads mutable exam rows, never calls an LLM and never creates a student
session.  That is the boundary which makes a printed paper reproducible and
auditable after the lesson has finished.
"""
from __future__ import annotations

import base64
from html import escape
from io import BytesIO
from pathlib import Path
from dataclasses import dataclass
from functools import lru_cache
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Iterable

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Flowable, Image, Paragraph, Table, TableStyle
from reportlab.lib.utils import ImageReader

from .print_package import (
    DEFAULT_OPEN_ANSWER_LINES,
    DEFAULT_OPEN_WORK_LINES,
    choice_option_identity,
)


PAGE_WIDTH, PAGE_HEIGHT = A4
MARGIN_X = 17 * mm
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN_X
TOP = PAGE_HEIGHT - 17 * mm
BOTTOM = 16 * mm
# The footer itself sits below ``BOTTOM``.  A compact 4 mm buffer here keeps
# task blocks clear of that footer while avoiding needless near-empty pages.
CONTENT_FOOTER_CLEARANCE = 4 * mm

NAVY = HexColor("#101B35")
BLUE = HexColor("#285BEA")
PURPLE = HexColor("#7043E8")
MUTED = HexColor("#64748B")
LINE = HexColor("#DCE4F1")
PAPER = HexColor("#F8FAFE")
GREEN = HexColor("#047857")
RED = HexColor("#C41C45")
GRID = HexColor("#DCE8F5")
GRID_CELL = 4.5 * mm
TASK_BADGE_RADIUS = 4.5 * mm
TASK_BADGE_FONT_SIZE = 8.8
TASK_FORMULA_FONT_SIZE = 12.5
KEY_FORMULA_FONT_SIZE = 12.5
# Formulae use the same KaTeX engine and font metrics as the draft UI.  A
# modest print-only increase keeps inline notation (for example ``f(x)`` and
# ``x_1``) comfortably legible beside the surrounding DejaVu text without
# changing the source LaTeX or its visual language.
# The paper is commonly viewed at less than 100% in the browser preview, so
# both prose and KaTeX receive a small, consistent print-size increase.
KATEX_PRINT_SCALE = 1.22
CHOICE_PROMPT_GAP = 5 * mm
SECTION_TASK_GAP = 7 * mm

_FONT_REGISTERED = False
_REGULAR = "Helvetica"
_BOLD = "Helvetica-Bold"
_ITALIC = "Helvetica-Oblique"
_ITALIC_FONT_REGISTERED = False


@dataclass(frozen=True)
class _MathArtifact:
    """A pre-rendered, cached formula that can be placed in the PDF canvas."""

    png: bytes
    width: float
    height: float


@dataclass(frozen=True)
class _FigureArtifact:
    """A verified task figure, scaled once for predictable page geometry."""

    reader: ImageReader
    width: float
    height: float


@lru_cache(maxsize=128)
def _embedded_figure(data_base64: str) -> _FigureArtifact:
    """Decode an image embedded in the protected print snapshot.

    The API validates the source type, byte size and signature before the
    bytes are stored.  Reopening a package therefore never fetches mutable
    external content and the PDF renderer remains deterministic.
    """
    try:
        raw = base64.b64decode(data_base64, validate=True)
        reader = ImageReader(BytesIO(raw))
        source_width, source_height = reader.getSize()
    except Exception as exc:
        raise ValueError("Не удалось прочитать встроенный рисунок задания") from exc
    if source_width <= 0 or source_height <= 0:
        raise ValueError("Рисунок задания имеет недопустимый размер")

    max_width = CONTENT_WIDTH - 14 * mm
    max_height = 82 * mm
    scale = min(max_width / source_width, max_height / source_height, 1.0)
    return _FigureArtifact(
        reader=reader,
        width=source_width * scale,
        height=source_height * scale,
    )


def _task_figure(task: dict[str, Any]) -> _FigureArtifact | None:
    """Return a package-owned task figure, never a mutable source URL."""
    embedded = task.get("question_image")
    if not isinstance(embedded, dict):
        return None
    data_base64 = str(embedded.get("data_base64") or "").strip()
    return _embedded_figure(data_base64) if data_base64 else None


def _hex_rgb(value: str) -> tuple[int, int, int]:
    """Turn the renderer's fixed CSS colours into RGB components."""
    normalized = value.lstrip("#")
    if len(normalized) != 6:
        raise ValueError(f"Unsupported formula colour: {value}")
    return tuple(int(normalized[index:index + 2], 16) for index in (0, 2, 4))


def _make_math_background_transparent(png: bytes, color: str) -> bytes:
    """Remove MathText's opaque white canvas without soft white boxes.

    ``math_to_image`` produces an opaque white PNG in some Matplotlib
    versions.  Placing it inside the light-blue answer table made every
    fraction look as if it had a separate white background.  Rebuilding a
    proper alpha channel from the known foreground colour preserves
    anti-aliased edges while allowing the table or page background through.
    """
    try:
        from PIL import Image as PillowImage

        image = PillowImage.open(BytesIO(png)).convert("RGBA")
        alpha = image.getchannel("A")
        # Newer Matplotlibs already provide a transparent canvas; do not
        # quantise their existing alpha mask.
        if alpha.getextrema()[0] < 255:
            return png

        foreground = _hex_rgb(color)
        converted = PillowImage.new("RGBA", image.size)
        source_pixels = image.load()
        target_pixels = converted.load()
        for y in range(image.height):
            for x in range(image.width):
                red, green, blue, original_alpha = source_pixels[x, y]
                coverage = max(
                    (255 - channel) / (255 - foreground_channel)
                    for channel, foreground_channel in zip(
                        (red, green, blue), foreground
                    )
                    if foreground_channel < 255
                )
                target_pixels[x, y] = (
                    foreground[0],
                    foreground[1],
                    foreground[2],
                    round(max(0.0, min(1.0, coverage)) * original_alpha),
                )
        buffer = BytesIO()
        converted.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        # Formula rendering remains best-effort.  A missing optional image
        # dependency must not stop teachers from producing a printable paper.
        return png


def _font_candidates(name: str) -> Iterable[Path]:
    """Prefer portable DejaVu on Linux and system Arial during local QA."""
    yield Path("/usr/share/fonts/truetype/dejavu") / name
    yield Path("/usr/local/share/fonts/truetype/dejavu") / name
    if name == "DejaVuSans-Oblique.ttf":
        # The slim Linux image carries the regular DejaVu weights for the
        # document. Liberation Sans Italic is bundled explicitly as the
        # compatible italic face used for a one-letter Latin parameter.
        yield Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Italic.ttf")
        yield Path("/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf")
    # macOS local development uses Arial; Docker receives DejaVu through the
    # service image.  Both include the Cyrillic glyphs required by ALGO.
    if name == "DejaVuSans.ttf":
        # Arial Unicode is present on the local macOS development machines and
        # prevents mathematical symbols such as ∈ and ℝ from turning into tofu
        # squares in a real printout. Docker uses DejaVu first.
        yield Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
        yield Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    if name == "DejaVuSans-Bold.ttf":
        yield Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf")
    if name == "DejaVuSans-Oblique.ttf":
        yield Path("/System/Library/Fonts/Supplemental/Arial Italic.ttf")


def _ensure_fonts() -> tuple[str, str]:
    global _FONT_REGISTERED, _REGULAR, _BOLD
    if _FONT_REGISTERED:
        return _REGULAR, _BOLD
    regular = next((path for path in _font_candidates("DejaVuSans.ttf") if path.exists()), None)
    bold = next((path for path in _font_candidates("DejaVuSans-Bold.ttf") if path.exists()), None)
    if regular and bold:
        pdfmetrics.registerFont(TTFont("AlgoPrint", str(regular)))
        pdfmetrics.registerFont(TTFont("AlgoPrint-Bold", str(bold)))
        _REGULAR, _BOLD = "AlgoPrint", "AlgoPrint-Bold"
    _FONT_REGISTERED = True
    return _REGULAR, _BOLD


def _ensure_italic_font() -> str:
    """Return the document's italic face for single-letter parameters.

    A lone variable in surrounding prose (for example ``$a$``) does not need
    a raster MathText image.  Drawing it with the matching italic document
    face keeps its cap height and baseline aligned with the Russian sentence
    in the exported PDF.
    """
    global _ITALIC, _ITALIC_FONT_REGISTERED
    if _ITALIC_FONT_REGISTERED:
        return _ITALIC

    italic = next(
        (path for path in _font_candidates("DejaVuSans-Oblique.ttf") if path.exists()),
        None,
    )
    if italic:
        pdfmetrics.registerFont(TTFont("AlgoPrint-Italic", str(italic)))
        _ITALIC = "AlgoPrint-Italic"
    _ITALIC_FONT_REGISTERED = True
    return _ITALIC


def _latex_to_print_text(value: str | None) -> str:
    """Produce a safe, readable mathematical fallback without a TeX runtime.

    The data path remains the verified LaTeX columns.  A small deterministic
    normalizer keeps the printed result legible for the supported expression
    set while the API does not pretend to provide a full TeX compiler.
    """
    source = (value or "").strip()
    if not source:
        return ""
    # A printable fallback must never expose TeX environment syntax.  Proper
    # typesetting is attempted below; this only protects historical malformed
    # input from leaking ``begin(cases)`` into a student handout.
    source = re.sub(
        r"\\begin\{cases\}(.*?)\\end\{cases\}",
        lambda match: "{ " + re.sub(r"\\\\", "; ", match.group(1)).replace("&", " ") + " }",
        source,
        flags=re.DOTALL,
    )
    source = source.replace("\\(", "").replace("\\)", "").replace("$$", "").replace("$", "")
    source = source.replace("\\left", "").replace("\\right", "")
    source = source.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")
    source = source.replace("\\,", " ").replace("\\;", " ").replace("\\!", "").replace("\\ ", " ")
    source = re.sub(
        r"\\(?:displaystyle|textstyle|mathrm|mathbf|mathbb|text)\{([^{}]*)\}",
        r"\1",
        source,
    )
    # Resolve simple fractions repeatedly from the innermost pair outwards.
    fraction = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
    while fraction.search(source):
        source = fraction.sub(lambda m: f"({m.group(1)})/({m.group(2)})", source)
    def readable_sqrt(match: re.Match[str]) -> str:
        radicand = match.group(1).strip()
        # ``√7`` is the conventional compact form in a coordinate answer.
        # Parentheses remain useful only for a compound radicand.
        return f"√{radicand}" if re.fullmatch(r"[A-Za-z0-9]", radicand) else f"√({radicand})"

    source = re.sub(r"\\sqrt(?:\[[^\]]+\])?\{([^{}]*)\}", readable_sqrt, source)
    replacements = {
        r"\cdot": "·", r"\times": "×", r"\div": "÷", r"\pm": "±",
        r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠",
        r"\approx": "≈", r"\infty": "∞", r"\cup": "∪", r"\cap": "∩",
        r"\in": "∈", r"\notin": "∉", r"\alpha": "α", r"\beta": "β",
        r"\gamma": "γ", r"\pi": "π", r"\theta": "θ", r"\sin": "sin ",
        r"\cos": "cos ", r"\tan": "tan ", r"\log": "log ",
    }
    for needle, replacement in replacements.items():
        source = source.replace(needle, replacement)
    def compact_fraction(match: re.Match[str]) -> str:
        numerator = match.group(1).strip()
        denominator = match.group(2).strip()
        common = {
            ("1", "2"): "½",
            ("1", "3"): "⅓",
            ("2", "3"): "⅔",
            ("1", "4"): "¼",
            ("3", "4"): "¾",
        }
        if (numerator, denominator) in common:
            return common[(numerator, denominator)]
        if re.fullmatch(r"[A-Za-zαβγπθ0-9]+", numerator) and re.fullmatch(
            r"[A-Za-zαβγπθ0-9]+", denominator
        ):
            return f"{numerator}/{denominator}"
        return match.group(0)

    # Readable fallbacks in mixed prose should not expose the implementation
    # parentheses that were only needed while recursively resolving TeX.
    source = re.sub(r"\(([^()]*)\)\s*/\s*\(([^()]*)\)", compact_fraction, source)
    source = re.sub(r"\\([A-Za-z]+)", r"\1", source)
    superscript = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")
    subscript = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
    source = re.sub(
        r"\^\{?([0-9+\-=()]+)\}?",
        lambda match: match.group(1).translate(superscript),
        source,
    )
    source = re.sub(
        r"_\{?([0-9+\-=()]+)\}?",
        lambda match: match.group(1).translate(subscript),
        source,
    )
    source = source.replace("{", "(").replace("}", ")")
    return re.sub(r"\s+", " ", source).strip()


def _formula_for_math_renderer(value: str | None) -> str:
    """Return source LaTeX unchanged, excluding only outer delimiters.

    The print representation is driven by the verified task-bank LaTeX. The
    PDF layer must not rewrite fractions, roots, delimiters, spacing commands,
    or styling macros before passing a formula to its renderer.
    """
    source = (value or "").strip()
    if not source:
        return ""
    source = source.removeprefix("$$").removesuffix("$$")
    source = source.removeprefix("$").removesuffix("$")
    source = source.removeprefix(r"\(").removesuffix(r"\)")
    if re.search(r"[А-Яа-яЁё]", source):
        return ""
    return source.strip()


def _mathtext_compatibility_fallback(source: str) -> str:
    """Retry only the three relation aliases missing in MathText.

    KaTeX accepts ``\\le``, ``\\ge`` and ``\\ne``; MathText accepts their
    long forms only. This token-bounded retry cannot touch ``\\left``.
    """
    source = re.sub(r"\\le(?=$|[^A-Za-z])", r"\\leq", source)
    source = re.sub(r"\\ge(?=$|[^A-Za-z])", r"\\geq", source)
    return re.sub(r"\\ne(?=$|[^A-Za-z])", r"\\neq", source)


_CASES_BLOCK = re.compile(r"^\s*\\begin\{cases\}(.*?)\\end\{cases\}\s*$", re.DOTALL)
_KATEX_RASTER_DPI = 288
# Starting Chromium dominates preview latency.  These rows remain safely
# below the screenshot-memory ceiling while reducing a 20-question preview
# from many browser launches to only a few.
_KATEX_BATCH_SIZE = 32
_KATEX_BATCH_ROW_HEIGHT = 112
_MATH_ARTIFACT_CACHE: dict[tuple[str, float, str], _MathArtifact | None] = {}


def _crop_transparent_math_png(png: bytes) -> bytes:
    """Crop a transparent browser screenshot to its mathematical glyphs."""
    try:
        from PIL import Image as PillowImage

        image = PillowImage.open(BytesIO(png)).convert("RGBA")
        bounds = image.getchannel("A").getbbox()
        if not bounds:
            return png
        padding = 10
        image = image.crop((
            max(0, bounds[0] - padding),
            max(0, bounds[1] - padding),
            min(image.width, bounds[2] + padding),
            min(image.height, bounds[3] + padding),
        ))
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return png


def _render_katex_artifacts(
    requests: list[tuple[str, float, str]],
) -> dict[tuple[str, float, str], _MathArtifact]:
    """Render several KaTeX formulae in one Chromium invocation per batch.

    The draft and PDF intentionally share KaTeX.  Rendering one short
    formula per browser process made a long preview take minutes, which left
    the preview tab blank while the request was still running.  Each batch
    keeps the exact same KaTeX HTML/CSS but places its rows in one page,
    then crops the transparent screenshot back into individual artifacts.
    """
    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "render_katex_html.cjs"
    stylesheet = project_root / "node_modules" / "katex" / "dist" / "katex.min.css"
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    node = shutil.which("node")
    if not script.is_file() or not stylesheet.is_file() or not chromium or not node:
        return {}

    unique_requests = list(dict.fromkeys(requests))
    rendered: dict[tuple[str, float, str], _MathArtifact] = {}

    try:
        with tempfile.TemporaryDirectory(prefix="algo-katex-") as temp_dir:
            temporary = Path(temp_dir)
            environment = {
                **os.environ,
                "KATEX_CSS_PATH": str(stylesheet),
            }
            for batch_index in range(0, len(unique_requests), _KATEX_BATCH_SIZE):
                batch = unique_requests[batch_index:batch_index + _KATEX_BATCH_SIZE]
                html = subprocess.run(
                    [node, str(script)],
                    input=json.dumps({
                        "formulas": [
                            {
                                "latex": latex,
                                "fontSize": font_size * KATEX_PRINT_SCALE,
                                "color": color,
                            }
                            for latex, font_size, color in batch
                        ],
                        "rowHeight": _KATEX_BATCH_ROW_HEIGHT,
                    }),
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=8,
                    env=environment,
                ).stdout
                html_path = temporary / f"formula-{batch_index}.html"
                png_path = temporary / f"formula-{batch_index}.png"
                browser_profile = temporary / f"chromium-profile-{batch_index}"
                browser_profile.mkdir()
                html_path.write_text(html, encoding="utf-8")
                subprocess.run(
                    [
                        chromium,
                        "--headless=new",
                        "--no-sandbox",
                        f"--user-data-dir={browser_profile}",
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--allow-file-access-from-files",
                        "--hide-scrollbars",
                        "--force-device-scale-factor=3",
                        "--default-background-color=00000000",
                        f"--window-size=1600,{_KATEX_BATCH_ROW_HEIGHT * len(batch)}",
                        f"--screenshot={png_path}",
                        html_path.as_uri(),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=20,
                )
                from PIL import Image as PillowImage

                screenshot = PillowImage.open(png_path).convert("RGBA")
                for row_index, request in enumerate(batch):
                    top = round(screenshot.height * row_index / len(batch))
                    bottom = round(screenshot.height * (row_index + 1) / len(batch))
                    row = screenshot.crop((0, top, screenshot.width, bottom))
                    row_buffer = BytesIO()
                    row.save(row_buffer, format="PNG")
                    png = _crop_transparent_math_png(
                        _make_math_background_transparent(row_buffer.getvalue(), request[2])
                    )
                    width_px, height_px = ImageReader(BytesIO(png)).getSize()
                    if width_px and height_px:
                        rendered[request] = _MathArtifact(
                            png=png,
                            width=width_px * 72 / _KATEX_RASTER_DPI,
                            height=height_px * 72 / _KATEX_RASTER_DPI,
                        )
    except Exception:
        return {}
    return rendered


def _render_katex_artifact(latex: str, font_size: float, color: str) -> _MathArtifact | None:
    """Render one source formula through the KaTeX batcher."""
    return _render_katex_artifacts([(latex, font_size, color)]).get((latex, font_size, color))


def _render_math_artifact(latex: str, font_size: float, color: str) -> _MathArtifact | None:
    """Render a verified formula to a transparent, print-resolution image.

    Rendering is intentionally best-effort.  A malformed historical formula
    or an unavailable optional graphics library must never make an exam
    impossible to print; callers always retain the readable text fallback.
    """
    source = (latex or "").strip()
    source = source.removeprefix("$$").removesuffix("$$")
    source = source.removeprefix("$").removesuffix("$")
    source = source.removeprefix(r"\(").removesuffix(r"\)")
    formula = _formula_for_math_renderer(source)
    if not formula:
        return None
    cache_key = (formula, float(font_size), color)
    if cache_key in _MATH_ARTIFACT_CACHE:
        return _MATH_ARTIFACT_CACHE[cache_key]
    katex = _render_katex_artifact(formula, font_size, color)
    if katex:
        _MATH_ARTIFACT_CACHE[cache_key] = katex
        return katex

    cases = _CASES_BLOCK.match(source)
    if cases:
        artifact = _render_cases_artifact(cases.group(1), font_size, color)
        _MATH_ARTIFACT_CACHE[cache_key] = artifact
        return artifact
    try:
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import math_to_image

        # 300 DPI keeps the formulae sharp on an ordinary office printer while
        # keeping the generated package small enough for several variants.
        dpi = 300
        def render_formula(candidate: str) -> bytes:
            buffer = BytesIO()
            math_to_image(
                f"${candidate}$",
                buffer,
                # Keep printed formulas on a predictable font family rather
                # than inheriting an environment-specific default font.
                prop=FontProperties(
                    family="DejaVu Sans",
                    style="normal",
                    weight="normal",
                    size=font_size,
                ),
                dpi=dpi,
                format="png",
                color=color,
            )
            return buffer.getvalue()

        try:
            rendered = render_formula(formula)
        except Exception:
            # Preserve the raw source on the normal path. This one retry only
            # bridges MathText's three known relation aliases.
            fallback = _mathtext_compatibility_fallback(formula)
            if fallback == formula:
                return None
            rendered = render_formula(fallback)
        png = _make_math_background_transparent(rendered, color)
        if not png:
            _MATH_ARTIFACT_CACHE[cache_key] = None
            return None
        width_px, height_px = ImageReader(BytesIO(png)).getSize()
        if not width_px or not height_px:
            return None
        artifact = _MathArtifact(
            png=png,
            width=width_px * 72 / dpi,
            height=height_px * 72 / dpi,
        )
        _MATH_ARTIFACT_CACHE[cache_key] = artifact
        return artifact
    except Exception:
        _MATH_ARTIFACT_CACHE[cache_key] = None
        return None


@lru_cache(maxsize=128)
def _render_cases_brace(height_px: int, color: str) -> Any | None:
    """Render the TeX STIX delimiter used for a multi-line system.

    A hand-drawn Bézier curve is easy to recognise as a brace but still looks
    unlike the assembled delimiter from professional mathematical typesetting.
    STIX supplies that exact TeX-style shape.  We crop its rendering and match
    it to the equation block height without changing its proportions.
    """
    try:
        from PIL import Image as PillowImage
        from matplotlib import rc_context
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import math_to_image

        dpi = 300
        font_size = max(10.0, height_px * 72 / dpi * 1.13)
        buffer = BytesIO()
        with rc_context({"mathtext.fontset": "stix"}):
            math_to_image(
                r"$\left\{\right.$",
                buffer,
                prop=FontProperties(
                    family="STIXGeneral",
                    style="normal",
                    weight="normal",
                    size=font_size,
                ),
                dpi=dpi,
                format="png",
                color=color,
            )
        image = PillowImage.open(
            BytesIO(_make_math_background_transparent(buffer.getvalue(), color))
        ).convert("RGBA")
        bounds = image.getchannel("A").getbbox()
        if not bounds:
            return None
        padding = 2
        image = image.crop((
            max(0, bounds[0] - padding),
            max(0, bounds[1] - padding),
            min(image.width, bounds[2] + padding),
            min(image.height, bounds[3] + padding),
        ))
        if image.height != height_px:
            resampling = getattr(PillowImage, "Resampling", PillowImage).LANCZOS
            width = max(1, round(image.width * height_px / image.height))
            image = image.resize((width, height_px), resampling)
        return image
    except Exception:
        return None


def _render_cases_artifact(source: str, font_size: float, color: str) -> _MathArtifact | None:
    """Typeset a school-level ``cases`` environment without a browser.

    Matplotlib MathText deliberately supports only a compact TeX subset and
    does not implement KaTeX's ``cases`` environment.  We still use the
    verified LaTex as-is: each mathematical row is typeset by MathText and a
    standard TeX STIX delimiter is composed beside them.  No content is
    parsed as an answer or changed in the process.
    """
    rows = [row.strip() for row in re.split(r"\\\\", source) if row.strip()]
    if not rows:
        return None
    rendered: list[_MathArtifact] = []
    for row in rows:
        # Alignment markers have semantic meaning only inside the TeX table;
        # in a single rendered row a generous mathematical gap is equivalent.
        row = row.replace("&", r"\quad ")
        artifact = _render_math_artifact(row, font_size, color)
        if artifact is None:
            return None
        rendered.append(artifact)
    try:
        from PIL import Image as PillowImage

        dpi = 300
        rendered_images = [PillowImage.open(BytesIO(item.png)).convert("RGBA") for item in rendered]
        gap_px = max(4, round(2.3 * dpi / 25.4))
        inner_height = sum(image.height for image in rendered_images) + gap_px * (len(rendered_images) - 1)
        brace_image = _render_cases_brace(inner_height, color)
        if brace_image is None:
            return None
        brace_gap_px = max(4, round(1.2 * dpi / 25.4))
        width = brace_image.width + brace_gap_px + max(image.width for image in rendered_images)
        image = PillowImage.new("RGBA", (width, inner_height), (255, 255, 255, 0))
        image.alpha_composite(brace_image, (0, 0))
        y = 0
        for row_image in rendered_images:
            image.alpha_composite(row_image, (brace_image.width + brace_gap_px, y))
            y += row_image.height + gap_px
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        png = buffer.getvalue()
        return _MathArtifact(png=png, width=width * 72 / dpi, height=inner_height * 72 / dpi)
    except Exception:
        return None


# A literal ``\(`` uses three regex backslashes: one to match the TeX
# backslash and one to escape the parenthesis.
_MATH_SEGMENT = re.compile(
    r"(\\begin\{cases\}.*?\\end\{cases\}|\$\$.*?\$\$|\$[^$]+\$|\\\(.*?\\\)|\\\[.*?\\\])",
    re.DOTALL,
)


def _is_undelimited_formula(value: str) -> bool:
    """Recognise a formula stored without ``$...$`` delimiters.

    Historical verified-LaTeX columns sometimes contain only ``\\frac`` or
    ``x^2 = 9``. They should receive the same typesetting as explicitly
    delimited maths. Regular English prompt text, on the other hand, must
    stay in the normal document font even if it happens to contain a number.
    """
    if _is_plain_math_value(value):
        return False
    formula = _formula_for_math_renderer(value)
    if not formula or re.search(
        r"\b(?:answer|calculate|choose|determine|find|question|solve)\b",
        formula,
        re.IGNORECASE,
    ):
        return False
    # Boolean answers are logical statements rather than algebraic display
    # formulae.  Keeping them in the document font prevents the variable in
    # ``x = False`` from being italic while the rest of the key is regular.
    if re.search(r"\b(?:True|False)\b", formula):
        return False
    # Bare numbers and coordinates are answer text, not display formulae.
    # Sending those values through MathText made e.g. ``7`` look italic and
    # larger than the neighbouring answer rows in the teacher key.  Keep the
    # typesetter for an actual equation, fraction, root, power or TeX command.
    return bool(re.search(r"\\|[=^_*/√≤≥≠≈∈∪∩]", formula))


def _classify_plain_lines(block: str) -> list[tuple[str, str]]:
    """Classify undelimited lines: clean formula lines versus normal prose.

    Historical verified rows frequently store the Russian wording and the
    formula on separate lines without ``$...$`` delimiters.  Per-line
    classification keeps prose in the document font while a clean formula line
    receives the same typesetting as an explicitly delimited one.
    """
    classified: list[tuple[str, str]] = []
    for line in block.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _is_undelimited_formula(stripped):
            classified.append(("math", stripped))
        else:
            # Do not trim plain prose.  This path is used for the text after
            # a `$...$` fragment; retaining its leading space prevents
            # constructs such as `значении α окружность` and `равна 45` from
            # being glued together in the printed document.
            classified.append(("text", line))
    return classified


def _is_plain_math_value(value: str) -> bool:
    """Recognise formula-marked values that should use the document font.

    Imports often wrap a scalar, a coordinate, a Greek parameter or a logical
    answer in ``$...$``.  It is still answer text, and rendering it as a MathText
    image makes it look italic, bolder or narrower than its neighbouring rows.
    Structured notation such as fractions, roots, powers and equations keeps
    the dedicated mathematical renderer.
    """
    source = (value or "").strip()
    source = source.removeprefix("$$").removesuffix("$$")
    source = source.removeprefix("$").removesuffix("$")
    source = source.removeprefix(r"\(").removesuffix(r"\)")
    source = source.replace(r"\left", "").replace(r"\right", "")
    source = source.replace(r"\,", " ").replace(r"\;", " ").replace(r"\!", "")
    if re.fullmatch(r"(?:\\(?:alpha|beta|gamma|pi|theta)|[αβγπθ])", source):
        return True
    if re.search(r"\b(?:True|False)\b", source):
        return True

    readable = _latex_to_print_text(source)
    if re.fullmatch(r"[+−-]?\d+(?:[.,]\d+)?", readable):
        return True
    # Numeric coordinates and lists of coordinates need no mathematical image.
    if re.fullmatch(r"[()\[\],;.\s+−\-\d]+", readable):
        return True
    # A named point, such as B(7; -1), is ordinary statement text as well.
    return bool(re.fullmatch(r"[A-Za-zА-Яа-я]\s*\([()\[\],;.\s+−\-\d]+\)", readable))


def _is_text_choice_value(value: str) -> bool:
    """Whether an elementary choice value should share the document font.

    Numeric coordinate sets containing only a simple square root (``√7``)
    are clearer and more consistent as document text.  Fractions, powers,
    equations and roots with compound radicands continue through MathText.
    """
    readable = _latex_to_print_text(value)
    return bool(re.fullmatch(r"[()\[\],;.\s+−\-√\d]+", readable))


def _is_single_latin_parameter(value: str) -> bool:
    """Recognise a TeX-delimited single Latin variable in running prose."""
    source = (value or "").strip()
    source = source.removeprefix("$$").removesuffix("$$")
    source = source.removeprefix("$").removesuffix("$")
    source = source.removeprefix(r"\(").removesuffix(r"\)")
    return bool(re.fullmatch(r"[A-Za-z]", source))


def _split_print_parts(value: str) -> list[tuple[str, str]]:
    """Split verified display prose into normal text and mathematical pieces."""
    source = (value or "").strip()
    if not source:
        return []
    parts: list[tuple[str, str]] = []
    cursor = 0
    for match in _MATH_SEGMENT.finditer(source):
        prose = source[cursor:match.start()]
        if prose.strip():
            parts.append(("text", prose))
        formula = match.group(0)
        if _is_plain_math_value(formula):
            parts.append(("text", _latex_to_print_text(formula)))
        elif _is_single_latin_parameter(formula):
            parts.append(("italic", _latex_to_print_text(formula)))
        elif _formula_for_math_renderer(formula):
            parts.append(("math", formula))
        else:
            parts.append(("text", _latex_to_print_text(formula)))
        cursor = match.end()
    if not parts:
        # A standalone source such as `\\frac{3}{4}` is a formula even when
        # the historical row omitted dollar delimiters. This branch must run
        # before adding a generic trailing-text fragment, otherwise fractions
        # degrade to the readable `(3)/(4)` fallback in answer options.
        classified = _classify_plain_lines(source)
        if classified:
            return classified
        return [("text", _latex_to_print_text(source))]

    remainder = source[cursor:]
    if remainder.strip():
        parts.extend(_classify_plain_lines(remainder))
    return parts


def _math_draw_size(artifact: _MathArtifact, max_width: float) -> tuple[float, float]:
    scale = min(1.0, max_width / artifact.width) if artifact.width else 1.0
    return artifact.width * scale, max(artifact.height * scale, 4.8 * mm)


def _mixed_text_height(
    value: str,
    style: ParagraphStyle,
    *,
    width: float,
    formula_size: float,
    formula_color: str,
) -> float:
    rows = _mixed_inline_rows(
        value,
        style,
        width=width,
        formula_size=formula_size,
        formula_color=formula_color,
    )
    return sum(row_height for row_height, _ in rows) + max(0, len(rows) - 1) * 1.5 * mm


def _mixed_inline_rows(
    value: str,
    style: ParagraphStyle,
    *,
    width: float,
    formula_size: float,
    formula_color: str,
) -> list[tuple[float, list[tuple[str, Any, float, float, float]]]]:
    """Lay prose and verified LaTeX fragments onto shared print lines.

    The renderer does not alter an exercise or its LaTeX.  It merely puts
    normal prose and already-verified formula images on the same baseline,
    matching the way KaTeX presents mixed content in the product UI.  This
    avoids the previous one-formula-per-line layout that made a short
    expression look like several disconnected words on paper.
    """
    regular, _ = _ensure_fonts()
    italic_font = _ensure_italic_font()
    font_name = style.fontName or regular
    font_size = float(style.fontSize)
    normal_height = max(float(style.leading), font_size * 1.25)
    space_width = pdfmetrics.stringWidth(" ", font_name, font_size)
    rows: list[tuple[float, list[tuple[str, Any, float, float, float]]]] = []
    current: list[tuple[str, Any, float, float, float]] = []
    used_width = 0.0
    row_height = normal_height
    pending_space = False

    def flush() -> None:
        nonlocal current, used_width, row_height, pending_space
        if current:
            rows.append((row_height, current))
        current = []
        used_width = 0.0
        row_height = normal_height
        pending_space = False

    def append_unit(kind: str, payload: Any, unit_width: float, unit_height: float) -> None:
        nonlocal used_width, row_height, pending_space
        gap = space_width if current and pending_space else 0.0
        if current and used_width + gap + unit_width > width:
            flush()
            gap = 0.0
        # A very wide display formula is scaled by `_math_draw_size`; keeping
        # a final fallback here prevents a corrupted legacy value from drawing
        # outside an A4 page.
        if unit_width > width:
            unit_width = width
        current.append((kind, payload, unit_width, unit_height, gap))
        used_width += gap + unit_width
        row_height = max(row_height, unit_height)
        pending_space = False

    def require_gap_before_math() -> None:
        """Keep prose and a following formula visually separate on paper.

        Some historical text joins a formula to the preceding word. The PDF
        renderer adds only a visual word boundary; it never changes the raw
        task or the stored LaTeX.
        """
        nonlocal pending_space
        if pending_space or not current:
            return
        previous_kind, previous_payload, *_ = current[-1]
        if previous_kind == "math":
            pending_space = True
            return
        if previous_kind not in {"text", "italic"}:
            return
        tail = str(previous_payload).rstrip()
        if tail and tail[-1] not in "=+-*/([{<":
            pending_space = True

    for kind, part in _split_print_parts(value):
        if kind == "math":
            artifact = _render_math_artifact(part, formula_size, formula_color)
            if artifact:
                artifact_width, artifact_height = _math_draw_size(artifact, width)
                require_gap_before_math()
                append_unit("math", artifact, artifact_width, artifact_height)
                # The next text token gets a boundary unless it starts with
                # punctuation, such as a period after a formula.
                pending_space = True
                continue
            # A malformed legacy fragment may not be supported by MathText.
            # Fall back to readable notation instead of exposing raw TeX.
            kind = "text"
            part = _latex_to_print_text(part)

        # ``part`` has already been converted to printable text by
        # `_split_print_parts` when needed.  Calling the normalizer again here
        # would strip the leading/trailing whitespace that separates an inline
        # scalar (for example `$45$`) or a Greek parameter from surrounding
        # Russian prose.
        for token in re.findall(r"\S+|\s+", str(part)):
            if token.isspace():
                pending_space = True
                continue
            if (
                pending_space
                and current
                and current[-1][0] == "math"
                and token[0] in ".,;:!?)]}%"
            ):
                pending_space = False
            token_kind = "italic" if kind == "italic" else "text"
            token_font = italic_font if token_kind == "italic" else font_name
            append_unit(
                token_kind,
                token,
                pdfmetrics.stringWidth(token, token_font, font_size),
                normal_height,
            )
    flush()
    return rows


def _draw_mixed_text(
    doc: "_PdfDocument",
    value: str,
    style: ParagraphStyle,
    *,
    width: float,
    indent: float = 0,
    formula_size: float = 11.0,
    formula_color: str = "#101B35",
) -> None:
    """Draw prose and real typeset math on a shared line flow."""
    rows = _mixed_inline_rows(
        value,
        style,
        width=width,
        formula_size=formula_size,
        formula_color=formula_color,
    )
    regular, _ = _ensure_fonts()
    italic_font = _ensure_italic_font()
    font_name = style.fontName or regular
    font_size = float(style.fontSize)
    for index, (row_height, units) in enumerate(rows):
        doc.ensure(row_height + 2 * mm)
        x = MARGIN_X + indent
        top = doc.y
        for kind, payload, unit_width, unit_height, gap in units:
            x += gap
            if kind == "math":
                image_y = top - (row_height - unit_height) / 2 - unit_height
                doc.canvas.drawImage(
                    ImageReader(BytesIO(payload.png)),
                    x,
                    image_y,
                    width=unit_width,
                    height=unit_height,
                    mask="auto",
                )
            else:
                baseline = top - (row_height - font_size) / 2 - font_size * 0.82
                doc.canvas.setFillColor(style.textColor)
                doc.canvas.setFont(
                    italic_font if kind == "italic" else font_name,
                    font_size,
                )
                doc.canvas.drawString(x, baseline, str(payload))
            x += unit_width
        doc.y -= row_height
        if index < len(rows) - 1:
            doc.y -= 1.5 * mm


class _MixedTextCell(Flowable):
    """A table cell that preserves inline LaTeX instead of flattening it.

    ReportLab paragraphs cannot draw a cached formula image among normal text.
    This flowable reuses the same line-breaking and baseline logic as a task
    condition, so source fragments such as ``$x_{1}$`` or ``$\\frac{\pi}{2}$``
    remain mathematical notation inside a long answer option.
    """

    def __init__(self, value: str, style: ParagraphStyle, *, formula_color: str) -> None:
        super().__init__()
        self.value = value
        self.style = style
        self.formula_color = formula_color
        self._rows: list[tuple[float, list[tuple[str, Any, float, float, float]]]] = []

    def wrap(self, available_width: float, available_height: float) -> tuple[float, float]:
        self._rows = _mixed_inline_rows(
            self.value,
            self.style,
            width=max(1.0, available_width),
            formula_size=float(self.style.fontSize),
            formula_color=self.formula_color,
        )
        height = sum(row_height for row_height, _ in self._rows)
        height += max(0, len(self._rows) - 1) * 1.5 * mm
        self.width = available_width
        self.height = height
        return available_width, height

    def draw(self) -> None:
        regular, _ = _ensure_fonts()
        italic_font = _ensure_italic_font()
        font_name = self.style.fontName or regular
        font_size = float(self.style.fontSize)
        top = self.height
        for row_index, (row_height, units) in enumerate(self._rows):
            x = 0.0
            for kind, payload, unit_width, unit_height, gap in units:
                x += gap
                if kind == "math":
                    image_y = top - (row_height - unit_height) / 2 - unit_height
                    self.canv.drawImage(
                        ImageReader(BytesIO(payload.png)),
                        x,
                        image_y,
                        width=unit_width,
                        height=unit_height,
                        mask="auto",
                    )
                else:
                    baseline = top - (row_height - font_size) / 2 - font_size * 0.82
                    self.canv.setFillColor(self.style.textColor)
                    self.canv.setFont(
                        italic_font if kind == "italic" else font_name,
                        font_size,
                    )
                    self.canv.drawString(x, baseline, str(payload))
                x += unit_width
            top -= row_height
            if row_index < len(self._rows) - 1:
                top -= 1.5 * mm


def _paragraph(text: str, style: ParagraphStyle) -> Paragraph:
    safe = escape(text).replace("\n", "<br/>")
    return Paragraph(safe, style)


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold = _ensure_fonts()
    styles = getSampleStyleSheet()
    return {
        "meta": ParagraphStyle("print-meta", parent=styles["Normal"], fontName=bold, fontSize=8.7, leading=11.7, textColor=MUTED, uppercase=True),
        "title": ParagraphStyle("print-title", parent=styles["Normal"], fontName=bold, fontSize=22, leading=26, textColor=NAVY),
        "subtitle": ParagraphStyle("print-subtitle", parent=styles["Normal"], fontName=regular, fontSize=10, leading=13.8, textColor=MUTED),
        "body": ParagraphStyle("print-body", parent=styles["Normal"], fontName=regular, fontSize=11.1, leading=15.3, textColor=NAVY),
        "task": ParagraphStyle("print-task", parent=styles["Normal"], fontName=regular, fontSize=11.6, leading=16.4, textColor=NAVY),
        "section": ParagraphStyle("print-section", parent=styles["Normal"], fontName=bold, fontSize=12.2, leading=15.6, textColor=NAVY),
        "task-number": ParagraphStyle("print-task-number", parent=styles["Normal"], fontName=bold, fontSize=11.4, leading=15.4, textColor=BLUE),
        "option": ParagraphStyle("print-option", parent=styles["Normal"], fontName=regular, fontSize=10.6, leading=14.5, textColor=NAVY),
        # Answers are identified by the green key colour, not by arbitrary
        # font weight.  A single regular weight keeps digits, words and
        # formula fallbacks visually consistent from row to row.
        "key": ParagraphStyle("print-key", parent=styles["Normal"], fontName=regular, fontSize=10.6, leading=14.5, textColor=GREEN),
    }


class _PdfDocument:
    def __init__(self, title: str, *, key: bool = False) -> None:
        self.buffer = BytesIO()
        self.canvas = canvas.Canvas(self.buffer, pagesize=A4, pageCompression=1)
        self.canvas.setTitle(title)
        self.canvas.setAuthor("ALGO")
        self.canvas.setSubject("Ключ преподавателя" if key else "Печатный бланк экзамена")
        self.styles = _styles()
        self.y = TOP
        self.page = 1
        self.key = key

    def _page_footer(self) -> None:
        regular, _ = _ensure_fonts()
        self.canvas.setStrokeColor(LINE)
        self.canvas.line(MARGIN_X, BOTTOM - 3 * mm, PAGE_WIDTH - MARGIN_X, BOTTOM - 3 * mm)
        self.canvas.setFillColor(MUTED)
        self.canvas.setFont(regular, 8)
        self.canvas.drawString(MARGIN_X, BOTTOM - 8 * mm, "ALGO · Печатный комплект экзамена")
        self.canvas.drawRightString(PAGE_WIDTH - MARGIN_X, BOTTOM - 8 * mm, f"Страница {self.page}")

    def next_page(self) -> None:
        self._page_footer()
        self.canvas.showPage()
        self.page += 1
        self.y = TOP

    def ensure(self, height: float) -> None:
        if self.y - height < BOTTOM + CONTENT_FOOTER_CLEARANCE:
            self.next_page()

    def draw_flowable(self, flowable: Any, width: float, *, indent: float = 0) -> float:
        available = self.y - BOTTOM - 12 * mm
        needed_width, needed_height = flowable.wrap(width, available)
        if needed_height > available:
            self.next_page()
            available = self.y - BOTTOM - 12 * mm
            needed_width, needed_height = flowable.wrap(width, available)
        flowable.drawOn(self.canvas, MARGIN_X + indent, self.y - needed_height)
        self.y -= needed_height
        return needed_height

    def finish(self) -> bytes:
        self._page_footer()
        self.canvas.save()
        return self.buffer.getvalue()


def _header(doc: _PdfDocument, snapshot: dict[str, Any], variant_number: int, *, key: bool) -> None:
    exam = snapshot["exam"]
    regular, bold = _ensure_fonts()
    doc.canvas.setFillColor(BLUE)
    doc.canvas.roundRect(MARGIN_X, doc.y - 11 * mm, 11 * mm, 11 * mm, 3 * mm, stroke=0, fill=1)
    doc.canvas.setFillColor(white)
    doc.canvas.setFont(bold, 15)
    doc.canvas.drawCentredString(MARGIN_X + 5.5 * mm, doc.y - 7.7 * mm, "Σ")
    doc.canvas.setFillColor(NAVY)
    doc.canvas.setFont(bold, 12)
    doc.canvas.drawString(MARGIN_X + 14 * mm, doc.y - 4.5 * mm, "ALGO")
    doc.canvas.setFillColor(MUTED)
    doc.canvas.setFont(regular, 8.2)
    doc.canvas.drawString(MARGIN_X + 14 * mm, doc.y - 8.7 * mm, "ПРОВЕРОЧНЫЙ МАТЕРИАЛ")
    doc.y -= 18 * mm
    doc.draw_flowable(_paragraph(exam["title"], doc.styles["title"]), CONTENT_WIDTH)
    doc.draw_flowable(_paragraph("Ключ преподавателя" if key else "Бланк ученика", doc.styles["subtitle"]), CONTENT_WIDTH)
    doc.y -= 4 * mm
    # Printed material is intentionally anonymous and reusable.  The group,
    # subject and time limit belong to the lesson record, not to a student's
    # answer paper.  The grade and the immutable variant are enough to
    # identify the correct form in the classroom.
    parts: list[str] = []
    if exam.get("class_level"):
        parts.append(f"{exam['class_level']} класс")
    parts.append(f"Вариант {variant_number}")
    doc.canvas.setFillColor(PAPER)
    doc.canvas.roundRect(MARGIN_X, doc.y - 9 * mm, CONTENT_WIDTH, 9 * mm, 2.5 * mm, stroke=0, fill=1)
    doc.canvas.setFillColor(MUTED)
    doc.canvas.setFont(bold, 8.4)
    doc.canvas.drawString(MARGIN_X + 4 * mm, doc.y - 5.8 * mm, "  •  ".join(parts))
    doc.y -= 15 * mm
    if not key:
        doc.canvas.setStrokeColor(LINE)
        doc.canvas.setLineWidth(0.7)
        label_y = doc.y - 2 * mm
        doc.canvas.setFillColor(MUTED)
        doc.canvas.setFont(regular, 8.3)
        doc.canvas.drawString(MARGIN_X, label_y, "ФИО ученика")
        doc.canvas.line(MARGIN_X + 25 * mm, label_y - 1 * mm, MARGIN_X + 103 * mm, label_y - 1 * mm)
        doc.canvas.drawString(MARGIN_X + 110 * mm, label_y, "Дата")
        doc.canvas.line(MARGIN_X + 122 * mm, label_y - 1 * mm, PAGE_WIDTH - MARGIN_X, label_y - 1 * mm)
        doc.y -= 11 * mm
    doc.canvas.setStrokeColor(NAVY)
    doc.canvas.setLineWidth(1.1)
    doc.canvas.line(MARGIN_X, doc.y, PAGE_WIDTH - MARGIN_X, doc.y)
    doc.y -= 8 * mm


def _task_text(task: dict[str, Any]) -> str:
    text = (task.get("question_text") or "").strip()
    latex = (task.get("question_latex") or "").strip()
    readable_latex = _latex_to_print_text(latex)
    # Backfill may place the whole mixed-language statement in a LaTeX field.
    # In that case prefer the normalized verified representation rather than
    # printing the entire condition twice. A short formula remains
    # supplemental to the human-readable wording.
    letter_count = len(re.findall(r"[А-Яа-яA-Za-z]", readable_latex))
    if latex and text and letter_count >= 12:
        return latex
    if latex and readable_latex not in _latex_to_print_text(text):
        return f"{text}\n{latex}".strip()
    return text or latex


def _draw_math_grid(
    doc: _PdfDocument,
    rows: int,
    *,
    indent: float = 13 * mm,
) -> None:
    """Draw a very light squared notebook area for handwritten maths.

    It is a visual aid only: no answer or source task is encoded in the
    grid.  The same fixed geometry is used for all open tasks, making a
    printed package predictable and independent from manual line settings.
    """
    rows = max(1, rows)
    height = rows * GRID_CELL
    doc.ensure(height + 5 * mm)
    left = MARGIN_X + indent
    right = PAGE_WIDTH - MARGIN_X
    bottom = doc.y - height
    doc.canvas.setStrokeColor(GRID)
    doc.canvas.setLineWidth(0.32)
    y = doc.y
    while y >= bottom - 0.1:
        doc.canvas.line(left, y, right, y)
        y -= GRID_CELL
    x = left
    while x <= right + 0.1:
        doc.canvas.line(x, doc.y, x, bottom)
        x += GRID_CELL
    doc.y = bottom - 3 * mm


def _task_badge_text_baseline(top: float, font_name: str) -> float:
    """Return the baseline that optically centres a task ordinal in its badge."""
    centre_y = top - TASK_BADGE_RADIUS
    ascent = pdfmetrics.getAscent(font_name, TASK_BADGE_FONT_SIZE)
    descent = pdfmetrics.getDescent(font_name, TASK_BADGE_FONT_SIZE)
    # ReportLab draws text from the baseline.  The vertical midpoint of the
    # actual font metrics is not the baseline, so subtract it from the badge
    # centre instead of using a magic "lower edge" offset.
    return centre_y - (ascent + descent) / 2


def _draw_task_badge(doc: _PdfDocument, ordinal: Any) -> None:
    """Draw one consistently centred blue ordinal badge."""
    centre_x = MARGIN_X + TASK_BADGE_RADIUS
    centre_y = doc.y - TASK_BADGE_RADIUS
    doc.canvas.setFillColor(BLUE)
    doc.canvas.circle(centre_x, centre_y, TASK_BADGE_RADIUS, stroke=0, fill=1)
    _, bold = _ensure_fonts()
    doc.canvas.setFillColor(white)
    doc.canvas.setFont(bold, TASK_BADGE_FONT_SIZE)
    doc.canvas.drawCentredString(
        centre_x,
        _task_badge_text_baseline(doc.y, bold),
        str(ordinal or ""),
    )


def _choice_table(
    doc: _PdfDocument,
    options: list[dict[str, str]],
    *,
    key: bool,
    correct_answer: str,
    correct_answer_latex: str = "",
) -> Table | None:
    if not options:
        return None
    rows: list[list[Any]] = []
    for option in options:
        is_correct = key and (
            choice_option_identity(
                str(option.get("value") or ""),
                str(option.get("latex") or ""),
            )
            == choice_option_identity(correct_answer, correct_answer_latex)
        )
        # A printed student sheet does not disclose correctness and does not
        # duplicate the answer-sheet circles. It uses only a clean letter.
        label = str(option.get("label", "?")).strip().rstrip(".")
        value = option.get("latex") or option.get("value") or ""
        style = doc.styles["key"] if is_correct else doc.styles["option"]
        # A bare scalar or a numeric coordinate list must use exactly the
        # same embedded document font as neighbouring answer rows.  Sending
        # even ``$1$`` through MathText made it look larger and heavier than
        # the fractions around it.  Fractions, roots and equations still keep
        # the dedicated crisp mathematical renderer.
        formula_source = (
            ""
            if _is_plain_math_value(value) or _is_text_choice_value(value)
            else _choice_option_formula(value)
        )
        formula = (
            _render_math_artifact(
                formula_source,
                float(style.fontSize),
                "#047857" if is_correct else "#101B35",
            )
            if formula_source
            else None
        )
        # Options are frequently pure formulae.  Rendering those formulae as
        # high-resolution MathText images keep fractions and nested powers crisp;
        # mixed prose retains the accessible embedded-font paragraph fallback.
        if formula and formula_source:
            formula_width, formula_height = _math_draw_size(formula, CONTENT_WIDTH - 32 * mm)
            option_cell: Any = Image(
                BytesIO(formula.png),
                width=formula_width,
                height=formula_height,
                hAlign="LEFT",
                mask="auto",
            )
        else:
            option_cell = _choice_text_cell(
                doc,
                value,
                style,
                formula_color="#047857" if is_correct else "#101B35",
            )
        rows.append([_paragraph(label, style), option_cell])
    # The answer must not visually stick to its letter marker.  The first
    # column reserves a stable marker block and the second has an explicit
    # inner gap, both in student papers and in the teacher key.
    table = Table(rows, colWidths=[14 * mm, CONTENT_WIDTH - 28 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PAPER),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, LINE),
        ("LEFTPADDING", (0, 0), (0, -1), 4 * mm),
        ("RIGHTPADDING", (0, 0), (0, -1), 1 * mm),
        ("LEFTPADDING", (1, 0), (1, -1), 6 * mm),
        ("RIGHTPADDING", (1, 0), (1, -1), 4 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return table


def _choice_text_cell(
    doc: _PdfDocument,
    value: str,
    style: ParagraphStyle,
    *,
    formula_color: str = "#101B35",
) -> Flowable:
    """Build a readable choice cell without discarding inline LaTeX."""
    cell_width = CONTENT_WIDTH - 38 * mm
    parts = _split_print_parts(value)
    if any(kind == "math" for kind, _ in parts):
        return _MixedTextCell(value, style, formula_color=formula_color)

    paragraph = _paragraph(_latex_to_print_text(value), style)
    _, height = paragraph.wrap(cell_width, PAGE_HEIGHT)
    if height <= 2 * float(style.leading):
        return paragraph
    compact_style = ParagraphStyle(
        f"{style.name}-compact",
        parent=style,
        fontSize=max(9.2, float(style.fontSize) - 0.4),
        leading=max(12.2, float(style.leading) - 0.6),
    )
    return _paragraph(value, compact_style)


def _strip_math_delimiters(value: str) -> str:
    """Return formula text without one pair of supported inline delimiters."""
    source = (value or "").strip()
    source = source.removeprefix("$$").removesuffix("$$")
    source = source.removeprefix("$").removesuffix("$")
    source = source.removeprefix(r"\(").removesuffix(r"\)")
    source = source.removeprefix(r"\[").removesuffix(r"\]")
    return source


def _choice_option_formula(value: str) -> str:
    """Join an option made entirely from formula fragments into one formula.

    Some imported multiple-choice rows store every coordinate as its own
    ``$...$`` fragment.  Falling back to a text paragraph then flattens each
    fraction into ``(2)/(3)`` inside the coordinate parentheses, causing the
    visible double brackets from the preview.  Joining mathematical fragments
    separated only by commas or semicolons preserves every coordinate while
    rendering their fractions consistently.
    """
    source = (value or "").strip()
    if not source:
        return ""
    matches = list(_MATH_SEGMENT.finditer(source))
    if not matches:
        # Historical rows may omit dollar delimiters altogether.  Keep the
        # existing formula safety checks before treating such a value as one
        # printable formula.
        return _formula_for_math_renderer(source)

    joined: list[str] = []
    cursor = 0
    for match in matches:
        separator = source[cursor:match.start()]
        if separator and not re.fullmatch(r"[\s,;]+", separator):
            return ""
        if separator:
            joined.append(separator)
        joined.append(_strip_math_delimiters(match.group(0)))
        cursor = match.end()
    remainder = source[cursor:]
    if remainder and not re.fullmatch(r"[\s,;]+", remainder):
        return ""
    if remainder:
        joined.append(remainder)
    return "".join(joined).strip()


def _draw_choice_options(
    doc: _PdfDocument,
    options: list[dict[str, str]],
    *,
    key: bool,
    correct_answer: str,
    correct_answer_latex: str = "",
) -> None:
    table = _choice_table(
        doc,
        options,
        key=key,
        correct_answer=correct_answer,
        correct_answer_latex=correct_answer_latex,
    )
    if table is None:
        return
    doc.draw_flowable(table, CONTENT_WIDTH - 14 * mm, indent=13 * mm)
    doc.y -= 3 * mm


def _variant_sections(variant: dict[str, Any]) -> list[dict[str, Any]]:
    """Return printed task sections, preserving older immutable snapshots.

    New package snapshots contain an explicit ``sections`` collection created
    from the same skill grouping as the draft editor.  Historical packages do
    not, and are deliberately kept in their original flat order.
    """
    sections = variant.get("sections")
    if isinstance(sections, list) and sections:
        valid = [section for section in sections if isinstance(section, dict) and isinstance(section.get("tasks"), list)]
        if valid:
            return valid
    return [{"tasks": variant.get("tasks") or []}]


def _section_title(section: dict[str, Any]) -> str:
    name = str(section.get("name") or "").strip()
    return name


def _section_kicker(section: dict[str, Any]) -> str:
    """A restrained section index, kept separate from the section title."""
    ordinal = section.get("ordinal")
    try:
        return f"РАЗДЕЛ {int(ordinal):02d}"
    except (TypeError, ValueError):
        return "РАЗДЕЛ"


def _section_height(doc: _PdfDocument, section: dict[str, Any]) -> float:
    title = _section_title(section)
    if not title:
        return 0.0
    height = _paragraph(title, doc.styles["section"]).wrap(CONTENT_WIDTH - 4 * mm, PAGE_HEIGHT)[1]
    return height + 5 * mm + SECTION_TASK_GAP + 0.5 * mm


def _draw_section_heading(doc: _PdfDocument, section: dict[str, Any]) -> None:
    """Draw a quiet editorial section header without a coloured banner."""
    title = _section_title(section)
    if not title:
        return
    flowable = _paragraph(title, doc.styles["section"])
    left = MARGIN_X + 4 * mm
    _, text_height = flowable.wrap(CONTENT_WIDTH - 4 * mm, PAGE_HEIGHT)
    divider_y = doc.y - text_height - 5 * mm

    # The short vertical rule and the small index create hierarchy without a
    # high-contrast banner competing with the actual exercise.
    doc.canvas.setFillColor(BLUE)
    doc.canvas.roundRect(MARGIN_X, divider_y, 0.9 * mm, text_height + 5 * mm, 0.45 * mm, stroke=0, fill=1)
    regular, _ = _ensure_fonts()
    doc.canvas.setFont(regular, 7.2)
    doc.canvas.drawString(left, doc.y - 2.5 * mm, _section_kicker(section))
    flowable.drawOn(doc.canvas, left, doc.y - text_height - 4.4 * mm)
    doc.canvas.setStrokeColor(LINE)
    doc.canvas.setLineWidth(0.45)
    doc.canvas.line(left, divider_y, PAGE_WIDTH - MARGIN_X, divider_y)
    doc.y = divider_y - SECTION_TASK_GAP


def _task_height(
    doc: _PdfDocument,
    task: dict[str, Any],
    question: str,
    *,
    key: bool,
    show_heading: bool = True,
) -> float:
    """Estimate a task as a whole to avoid orphaning its workspace on a new page."""
    content_width = CONTENT_WIDTH - 14 * mm
    height = 0.0
    if show_heading:
        heading = task.get("skill_name") or f"Задание {task.get('ordinal', '')}"
        height = _paragraph(heading, doc.styles["meta"]).wrap(content_width, PAGE_HEIGHT)[1]
    height += _mixed_text_height(
        question,
        doc.styles["task"],
        width=content_width,
        formula_size=TASK_FORMULA_FONT_SIZE,
        formula_color="#101B35",
    )
    figure = _task_figure(task)
    if figure is not None:
        height += 3 * mm + figure.height + 3 * mm
    height += CHOICE_PROMPT_GAP if task.get("response_kind") == "choice" else 3 * mm
    response_kind = task.get("response_kind")
    if response_kind == "choice":
        table = _choice_table(
            doc,
            task.get("options") or [],
            key=key,
            correct_answer=task.get("correct_answer", ""),
            correct_answer_latex=task.get("correct_answer_latex", ""),
        )
        if table is not None:
            height += table.wrap(content_width, PAGE_HEIGHT)[1] + 3 * mm
    elif not key:
        height += 6 * mm + DEFAULT_OPEN_WORK_LINES * GRID_CELL + 3 * mm
    return height + 7 * mm


def _draw_task(doc: _PdfDocument, task: dict[str, Any], *, key: bool, show_heading: bool = True) -> None:
    question = _task_text(task)
    response_kind = task.get("response_kind")
    answer_label = "Ответ" if response_kind == "open" else "Выберите один ответ"
    # Keep the heading, prompt and the teacher-selected workspace together.
    # A continuation page without the question is too easy to misread on paper.
    doc.ensure(_task_height(doc, task, question, key=key, show_heading=show_heading))
    _draw_task_badge(doc, task.get("ordinal"))
    if show_heading:
        heading = task.get("skill_name") or f"Задание {task.get('ordinal', '')}"
        doc.draw_flowable(_paragraph(heading, doc.styles["meta"]), CONTENT_WIDTH - 14 * mm, indent=13 * mm)
    _draw_mixed_text(
        doc,
        question,
        doc.styles["task"],
        width=CONTENT_WIDTH - 14 * mm,
        indent=13 * mm,
        formula_size=TASK_FORMULA_FONT_SIZE,
    )
    figure = _task_figure(task)
    if figure is not None:
        doc.y -= 3 * mm
        content_x = MARGIN_X + 13 * mm
        content_width = CONTENT_WIDTH - 14 * mm
        figure_x = content_x + (content_width - figure.width) / 2
        doc.canvas.drawImage(
            figure.reader,
            figure_x,
            doc.y - figure.height,
            width=figure.width,
            height=figure.height,
            preserveAspectRatio=True,
            mask="auto",
        )
        doc.y -= figure.height + 3 * mm
    if response_kind == "choice":
        doc.y -= CHOICE_PROMPT_GAP
        _draw_choice_options(
            doc,
            task.get("options") or [],
            key=key,
            correct_answer=task.get("correct_answer", ""),
            correct_answer_latex=task.get("correct_answer_latex", ""),
        )
    elif not key:
        doc.y -= 3 * mm
        doc.canvas.setFillColor(MUTED)
        regular, _ = _ensure_fonts()
        doc.canvas.setFont(regular, 8.2)
        doc.canvas.drawString(MARGIN_X + 13 * mm, doc.y - 2 * mm, "Решение")
        doc.y -= 5 * mm
        _draw_math_grid(doc, DEFAULT_OPEN_WORK_LINES)
    if response_kind != "open":
        doc.canvas.setStrokeColor(LINE)
        doc.canvas.setLineWidth(0.55)
        doc.canvas.line(MARGIN_X, doc.y, PAGE_WIDTH - MARGIN_X, doc.y)
    doc.y -= 7 * mm


def _draw_student_sections(doc: _PdfDocument, variant: dict[str, Any]) -> None:
    """Render grouped draft sections without changing online task delivery."""
    for section in _variant_sections(variant):
        tasks = [task for task in section.get("tasks") or [] if isinstance(task, dict)]
        if not tasks:
            continue
        title = _section_title(section)
        if title:
            first_question = _task_text(tasks[0])
            doc.ensure(
                _section_height(doc, section)
                + _task_height(doc, tasks[0], first_question, key=False, show_heading=False)
            )
            _draw_section_heading(doc, section)
        for task_index, task in enumerate(tasks):
            # A section has one visual heading only.  Tasks may continue on a
            # new page, but the section name must not repeat as a second,
            # misleading section start.
            if task_index and title:
                question = _task_text(task)
                doc.ensure(_task_height(doc, task, question, key=False, show_heading=False))
            _draw_task(doc, task, key=False, show_heading=not bool(title))


def _draw_answer_sheet(doc: _PdfDocument, variant: dict[str, Any]) -> None:
    choices = [task for task in variant.get("tasks", []) if task.get("response_kind") == "choice"]
    open_answers = [task for task in variant.get("tasks", []) if task.get("response_kind") == "open"]
    if not choices and not open_answers:
        return
    doc.next_page()
    doc.draw_flowable(_paragraph("Лист ответов", doc.styles["title"]), CONTENT_WIDTH)
    doc.draw_flowable(_paragraph("Отметьте один вариант в каждой строке. Этот лист можно использовать для быстрой ручной проверки.", doc.styles["subtitle"]), CONTENT_WIDTH)
    doc.y -= 8 * mm
    if choices:
        labels = sorted({option.get("label", "") for task in choices for option in task.get("options", [])})
        header = [_paragraph("№", doc.styles["meta"])] + [_paragraph(label, doc.styles["meta"]) for label in labels]
        rows = [header]
        for task in choices:
            allowed = {option.get("label") for option in task.get("options", [])}
            row = [_paragraph(str(task.get("ordinal")), doc.styles["body"])]
            row.extend(_paragraph("○" if label in allowed else "—", doc.styles["body"]) for label in labels)
            rows.append(row)
        col_widths = [14 * mm] + [(CONTENT_WIDTH - 14 * mm) / max(len(labels), 1)] * len(labels)
        table = Table(rows, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#ECF2FF")),
            ("GRID", (0, 0), (-1, -1), 0.5, LINE),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            # A compact answer card must keep its title and all choice rows
            # on the same sheet.  Larger padding made a 20-question card
            # spill the table onto the next page, leaving its heading alone.
            ("TOPPADDING", (0, 0), (-1, -1), 2.0 * mm),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.0 * mm),
        ]))
        doc.draw_flowable(table, CONTENT_WIDTH)
        doc.y -= 7 * mm
    if open_answers:
        doc.draw_flowable(_paragraph("Открытые ответы", doc.styles["meta"]), CONTENT_WIDTH)
        doc.y -= 2 * mm
        for task in open_answers:
            doc.ensure(DEFAULT_OPEN_ANSWER_LINES * GRID_CELL + 12 * mm)
            doc.canvas.setFillColor(NAVY)
            _, bold = _ensure_fonts()
            doc.canvas.setFont(bold, 9.5)
            doc.canvas.drawString(MARGIN_X, doc.y - 2 * mm, f"{task.get('ordinal')}. Ответ")
            doc.y -= 5 * mm
            _draw_math_grid(doc, DEFAULT_OPEN_ANSWER_LINES, indent=0)


def _teacher_choice_label(task: dict[str, Any]) -> str:
    """Return the fixed printed letter for a correct multiple-choice option."""
    correct_identity = choice_option_identity(
        str(task.get("correct_answer") or ""),
        str(task.get("correct_answer_latex") or ""),
    )
    if not correct_identity:
        return ""
    for option in task.get("options") or []:
        if not isinstance(option, dict):
            continue
        if choice_option_identity(
            str(option.get("value") or ""),
            str(option.get("latex") or ""),
        ) == correct_identity:
            return str(option.get("label") or "").strip().rstrip(".")
    return ""


def _teacher_key_value_offset(doc: _PdfDocument, value: str) -> float:
    """Return the top inset that centres a one-line key value on its badge."""
    height = _mixed_text_height(
        value,
        doc.styles["key"],
        width=CONTENT_WIDTH - 13 * mm,
        formula_size=KEY_FORMULA_FONT_SIZE,
        formula_color="#047857",
    )
    return max(0.0, TASK_BADGE_RADIUS - height / 2)


def _draw_teacher_key(doc: _PdfDocument, variant: dict[str, Any]) -> None:
    """Draw a compact answer-only key; task conditions never repeat here."""
    doc.draw_flowable(
        _paragraph("Ответы по заданиям. Вариант сверяется с номером на бланке ученика.", doc.styles["subtitle"]),
        CONTENT_WIDTH,
    )
    doc.y -= 6 * mm
    # The teacher key remains deliberately compact: section labels are shown
    # on the student paper, while this protected answer-only sheet preserves
    # a scan-friendly one-row-per-task format.
    for task in variant.get("tasks", []):
        is_choice = task.get("response_kind") == "choice"
        correct = task.get("correct_answer_latex") or task.get("correct_answer") or ""
        choice_label = _teacher_choice_label(task) if is_choice else ""
        displayed_answer = choice_label or "—" if is_choice else correct
        doc.ensure(16 * mm)
        row_top = doc.y
        _draw_task_badge(doc, task.get("ordinal"))
        # A row needs only the ordinal and its result.  Choice tasks return a
        # printed letter; open tasks retain their authored answer value.
        doc.y -= _teacher_key_value_offset(doc, str(displayed_answer or "—"))
        _draw_mixed_text(
            doc,
            str(displayed_answer or "—"),
            doc.styles["key"],
            width=CONTENT_WIDTH - 13 * mm,
            indent=13 * mm,
            formula_size=KEY_FORMULA_FONT_SIZE,
            formula_color="#047857",
        )
        # Keep the separator below the 9 mm ordinal circle.
        doc.y = min(doc.y, row_top - 10.5 * mm) - 2 * mm
        doc.canvas.setStrokeColor(LINE)
        doc.canvas.setLineWidth(0.55)
        doc.canvas.line(MARGIN_X, doc.y, PAGE_WIDTH - MARGIN_X, doc.y)
        doc.y -= 5 * mm


def _formula_requests_from_value(
    value: str,
    *,
    font_size: float,
    color: str,
) -> list[tuple[str, float, str]]:
    """Collect KaTeX fragments exactly as the mixed-text drawer will use them."""
    requests: list[tuple[str, float, str]] = []
    for kind, fragment in _split_print_parts(value):
        if kind != "math":
            continue
        formula = _formula_for_math_renderer(fragment)
        if formula:
            requests.append((formula, float(font_size), color))
    return requests


def _prewarm_variant_math(doc: _PdfDocument, variant: dict[str, Any], *, key: bool) -> None:
    """Build all KaTeX artifacts before laying out a PDF page.

    ReportLab needs the final formula dimensions to decide line breaks.  A
    small preflight therefore batches the known fragments once, avoiding a
    slow Chromium launch for every formula while preserving the exact values
    later drawn into the student paper or teacher key.
    """
    requests: list[tuple[str, float, str]] = []
    if key:
        for task in variant.get("tasks") or []:
            if task.get("response_kind") == "choice":
                continue
            displayed = task.get("correct_answer_latex") or task.get("correct_answer") or ""
            requests.extend(_formula_requests_from_value(
                str(displayed),
                font_size=KEY_FORMULA_FONT_SIZE,
                color="#047857",
            ))
    else:
        option_font_size = float(doc.styles["option"].fontSize)
        for task in variant.get("tasks") or []:
            requests.extend(_formula_requests_from_value(
                _task_text(task),
                font_size=TASK_FORMULA_FONT_SIZE,
                color="#101B35",
            ))
            if task.get("response_kind") != "choice":
                continue
            for option in task.get("options") or []:
                value = str(option.get("latex") or option.get("value") or "")
                formula_source = (
                    ""
                    if _is_plain_math_value(value) or _is_text_choice_value(value)
                    else _choice_option_formula(value)
                )
                if formula_source:
                    formula = _formula_for_math_renderer(formula_source)
                    if formula:
                        requests.append((formula, option_font_size, "#101B35"))
                else:
                    requests.extend(_formula_requests_from_value(
                        value,
                        font_size=option_font_size,
                        color="#101B35",
                    ))

    pending = [
        request
        for request in dict.fromkeys(requests)
        if request not in _MATH_ARTIFACT_CACHE
    ]
    if not pending:
        return
    for request, artifact in _render_katex_artifacts(pending).items():
        _MATH_ARTIFACT_CACHE[request] = artifact


def _render(snapshot: dict[str, Any], variant_number: int, *, key: bool) -> bytes:
    variants = {int(v.get("variant_number", 0)): v for v in snapshot.get("variants", [])}
    variant = variants.get(int(variant_number))
    if not variant:
        raise ValueError(f"В подготовленном комплекте нет варианта №{variant_number}")
    title = snapshot.get("exam", {}).get("title") or "Экзамен"
    doc = _PdfDocument(title, key=key)
    _prewarm_variant_math(doc, variant, key=key)
    _header(doc, snapshot, variant_number, key=key)
    if key:
        _draw_teacher_key(doc, variant)
    else:
        _draw_student_sections(doc, variant)
    if not key:
        _draw_answer_sheet(doc, variant)
    return doc.finish()


def render_student_exam_pdf(snapshot: dict[str, Any], variant_number: int) -> bytes:
    """Render a printable student paper without answers or answer-key hints."""
    return _render(snapshot, variant_number, key=False)


def render_teacher_key_pdf(snapshot: dict[str, Any], variant_number: int) -> bytes:
    """Render a separate teacher-only key for one immutable paper variant."""
    return _render(snapshot, variant_number, key=True)
