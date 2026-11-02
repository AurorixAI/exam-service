"""WC-B EX-48: the formula and figure caches of the print renderer are bounded (a worker lives for weeks; the old dict grew with every formula
and the figure cache kept the base64 text of up to 8 MB as its key).  Behaviour of the renderer is the same: a hit returns the same artifact."""
from __future__ import annotations

import base64
from io import BytesIO

from src.domain import print_pdf


def test_the_formula_cache_never_grows_past_its_bound_and_forgets_the_oldest_first():
    cache = print_pdf._BoundedCache(2000)
    for i in range(2500):
        cache[(f"x^{i}", 12.0, "#000")] = i
    assert len(cache) == 2000
    assert ("x^0", 12.0, "#000") not in cache and ("x^499", 12.0, "#000") not in cache
    assert ("x^500", 12.0, "#000") in cache and cache[("x^2499", 12.0, "#000")] == 2499
    print(f"\nEX-48 formula cache after 2500 distinct formulas: {len(cache)} entries (was 2500 and growing)")


def test_a_hit_keeps_the_entry_alive():
    cache = print_pdf._BoundedCache(3)
    for key in "abc":
        cache[key] = key
    assert cache["a"] == "a"          # «a» is used again: «b» is now the oldest
    cache["d"] = "d"
    assert "b" not in cache and all(k in cache for k in "acd")


def test_the_renderer_uses_the_bounded_cache():
    assert isinstance(print_pdf._MATH_ARTIFACT_CACHE, print_pdf._BoundedCache) and print_pdf._MATH_ARTIFACT_CACHE.maxsize == 2000
    assert print_pdf._FIGURE_CACHE.maxsize == 32


def png_b64(width, height, shade):
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (shade, shade, shade)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_figures_are_cached_by_digest_and_bounded():
    print_pdf._FIGURE_CACHE.clear()
    first = print_pdf._embedded_figure(png_b64(400, 200, 10))
    assert print_pdf._embedded_figure(png_b64(400, 200, 10)) is first       # same picture, same decoded artifact
    assert (first.width, first.height) == (first.width, first.width / 2)    # geometry as before: scaled to the page, the ratio kept
    for shade in range(11, 60):
        print_pdf._embedded_figure(png_b64(40, 20, shade))
    assert len(print_pdf._FIGURE_CACHE) == 32
    keys = list(print_pdf._FIGURE_CACHE._items)
    assert all(len(k) == 64 and set(k) <= set("0123456789abcdef") for k in keys)     # digests: the base64 text is not kept as a key
    print(f"EX-48 figure cache after 50 distinct pictures: {len(print_pdf._FIGURE_CACHE)} entries of 64-char keys (was 128 keys of the full base64 text)")


def test_a_broken_figure_is_still_refused_and_not_cached():
    print_pdf._FIGURE_CACHE.clear()
    for _ in range(2):
        try:
            print_pdf._embedded_figure("not base64!!")
        except ValueError as exc:
            assert "рисунок" in str(exc)
        else:
            raise AssertionError("must refuse")
    assert len(print_pdf._FIGURE_CACHE) == 0
