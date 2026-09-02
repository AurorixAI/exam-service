"""Repair the known malformed derivative option in safe exam snapshots.

The source task is corrected by content-service.  Generated exams deliberately
contain a copy of their tasks, so editable drafts and unreleased print packages
need the same narrowly scoped repair.  Released, active, closed and archived
packages are intentionally left untouched: their historical paper must remain
provable from its original hash.

An unreleased package is corrected as a documented content revision: its hash
is recalculated, its version is incremented, and an audit event records both
hashes.  This preserves traceability while ensuring that no teacher prints a
known-broken formula.

Revision ID: 015_repair_derivative_distractor
Revises: 014_add_print_exam_packages
Create Date: 2026-08-30
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from uuid import uuid4

import sqlalchemy as sa
from alembic import op


revision = "015_repair_derivative_distractor"
down_revision = "014_add_print_exam_packages"
branch_labels = None
depends_on = None


TASK_ID = "G11_TB_4_6_4_48_6"
LEGACY_VALUE = r"$25x^{24} \cdot 4\cos(x) - $x^{25}$ \cdot 4\sin(x)$"
CORRECTED_VALUE = r"$25x^{24} \cdot \cos(x) - x^{25} \cdot \sin(x)$"
LEGACY_LOGIC = (
    "Ученик применил правило произведения, но забыл умножить производную "
    "степенной функции на второй множитель: вместо $25*4=100$ оставил "
    "$25*4$ как отдельные множители, не выполнив умножение коэффициентов."
)
LEGACY_LOGIC_LATEX = (
    "Ученик применил правило произведения, но забыл умножить производную "
    "степенной функции на второй множитель: вместо $25 \\cdot 4 = 100$ "
    "оставил $25 \\cdot 4$ как отдельные множители, не выполнив умножение коэффициентов."
)
CORRECTED_LOGIC = (
    "Ученик применил правило произведения, но потерял постоянный множитель 4 "
    "в обоих слагаемых производной."
)


def _canonical_hash(snapshot: dict[str, object]) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _patch_distractor_metadata(task: dict[str, object], *, restore_legacy: bool) -> bool:
    if task.get("task_id") != TASK_ID:
        return False
    metadata = task.get("distractor_meta")
    if not isinstance(metadata, list) or len(metadata) < 2 or not isinstance(metadata[1], dict):
        return False
    item = metadata[1]
    expected = CORRECTED_VALUE if restore_legacy else LEGACY_VALUE
    if item.get("value") != expected or item.get("value_latex") != expected:
        return False

    if restore_legacy:
        item.update(
            value=LEGACY_VALUE,
            value_latex=LEGACY_VALUE,
            error_logic=LEGACY_LOGIC,
            explanation=LEGACY_LOGIC,
            error_logic_latex=LEGACY_LOGIC_LATEX,
        )
    else:
        item.update(
            value=CORRECTED_VALUE,
            value_latex=CORRECTED_VALUE,
            error_logic=CORRECTED_LOGIC,
            explanation=CORRECTED_LOGIC,
            error_logic_latex=CORRECTED_LOGIC,
        )
    return True


def _patch_variants(variants: object, *, restore_legacy: bool) -> tuple[list[dict[str, object]], bool]:
    if not isinstance(variants, list):
        return [], False
    patched_variants = deepcopy(variants)
    changed = False
    for variant in patched_variants:
        if not isinstance(variant, dict):
            continue
        tasks = variant.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if isinstance(task, dict):
                changed = _patch_distractor_metadata(task, restore_legacy=restore_legacy) or changed
    return patched_variants, changed


def _patch_snapshot_task(task: object, *, restore_legacy: bool) -> bool:
    if not isinstance(task, dict) or task.get("task_id") != TASK_ID:
        return False
    options = task.get("options")
    if not isinstance(options, list):
        return False
    expected = CORRECTED_VALUE if restore_legacy else LEGACY_VALUE
    replacement = LEGACY_VALUE if restore_legacy else CORRECTED_VALUE
    changed = False
    for option in options:
        if not isinstance(option, dict) or option.get("value") != expected:
            continue
        option["value"] = replacement
        if option.get("latex") == expected:
            option["latex"] = replacement
        changed = True
    return changed


def _patch_snapshot(snapshot: object, *, restore_legacy: bool) -> tuple[dict[str, object], bool]:
    if not isinstance(snapshot, dict):
        return {}, False
    patched = deepcopy(snapshot)
    changed = False
    variants = patched.get("variants")
    if not isinstance(variants, list):
        return patched, False
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        for task in variant.get("tasks") or []:
            changed = _patch_snapshot_task(task, restore_legacy=restore_legacy) or changed
        for section in variant.get("sections") or []:
            if not isinstance(section, dict):
                continue
            for task in section.get("tasks") or []:
                changed = _patch_snapshot_task(task, restore_legacy=restore_legacy) or changed
    return patched, changed


def _write_variants(bind: sa.Connection, *, restore_legacy: bool) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT e.id, e.variants
            FROM generated_exams e
            LEFT JOIN exam_print_packages p ON p.exam_id = e.id
            WHERE e.status IN ('draft', 'variants_ready')
               OR (p.status = 'ready' AND p.released_at IS NULL)
            FOR UPDATE OF e
            """
        )
    ).mappings()
    for row in rows:
        variants, changed = _patch_variants(row["variants"], restore_legacy=restore_legacy)
        if not changed:
            continue
        bind.execute(
            sa.text(
                """
                UPDATE generated_exams
                SET variants = CAST(:variants AS jsonb), updated_at = NOW()
                WHERE id = :exam_id
                """
            ),
            {"exam_id": row["id"], "variants": json.dumps(variants, ensure_ascii=False)},
        )


def _write_unreleased_packages(bind: sa.Connection, *, restore_legacy: bool) -> None:
    rows = bind.execute(
        sa.text(
            """
            SELECT p.id, p.exam_id, p.org_id, p.version, p.snapshot, p.snapshot_sha256
            FROM exam_print_packages p
            WHERE p.status = 'ready' AND p.released_at IS NULL
            FOR UPDATE
            """
        )
    ).mappings()
    for row in rows:
        snapshot, changed = _patch_snapshot(row["snapshot"], restore_legacy=restore_legacy)
        if not changed:
            continue
        previous_hash = str(row["snapshot_sha256"])
        new_hash = _canonical_hash(snapshot)
        bind.execute(
            sa.text(
                """
                UPDATE exam_print_packages
                SET snapshot = CAST(:snapshot AS jsonb),
                    snapshot_sha256 = :snapshot_sha256,
                    version = :version
                WHERE id = :package_id
                """
            ),
            {
                "package_id": row["id"],
                "snapshot": json.dumps(snapshot, ensure_ascii=False),
                "snapshot_sha256": new_hash,
                "version": int(row["version"] or 1) + 1,
            },
        )
        bind.execute(
            sa.text(
                """
                INSERT INTO exam_lifecycle_events (
                    id, exam_id, org_id, event_type, reason, details
                ) VALUES (
                    :id, :exam_id, :org_id, :event_type, :reason,
                    CAST(:details AS jsonb)
                )
                """
            ),
            {
                "id": uuid4().hex,
                "exam_id": row["exam_id"],
                "org_id": row["org_id"],
                "event_type": "print_package_content_correction",
                "reason": "Исправлена известная разорванная LaTeX-разметка варианта ответа до начала экзамена",
                "details": json.dumps(
                    {
                        "task_id": TASK_ID,
                        "previous_snapshot_sha256": previous_hash,
                        "snapshot_sha256": new_hash,
                        "content_revision": "derivative_distractor_latex_repair",
                    },
                    ensure_ascii=False,
                ),
            },
        )


def _apply(*, restore_legacy: bool) -> None:
    bind = op.get_bind()
    _write_variants(bind, restore_legacy=restore_legacy)
    _write_unreleased_packages(bind, restore_legacy=restore_legacy)


def upgrade() -> None:
    _apply(restore_legacy=False)


def downgrade() -> None:
    _apply(restore_legacy=True)
