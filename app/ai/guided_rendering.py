"""Current-app wrapper around the developer guided Gemini render pipeline."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from PIL import Image

from app.ai import verification
from app.ai.config import ai_settings
from app.ai.floorplan_analyzer import analyze_floorplan, analyze_floorplan_verified
from app.ai.gemini_image_service import photorealize
from app.ai.image_preparation_service import prepare_image
from app.ai.prompts import RENDER_MODES, build_render_prompt
from app.ai.style_reference_provider import (
    asset_style_references,
    bundled_sketchup_reference,
    guess_mime,
)
from app.ai_service import Generate3DResult


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _normalize_mode(mode: Optional[str], style: Optional[str]) -> str:
    candidate = (mode or "").strip().lower()
    if candidate in RENDER_MODES:
        return candidate
    style_candidate = (style or "").strip().lower()
    if style_candidate in RENDER_MODES:
        return style_candidate
    return "sketchup"


def _read_required(path: str, label: str) -> bytes:
    if not path:
        raise ValueError(f"Missing {label}.")
    if not os.path.isfile(path):
        raise ValueError(f"{label} does not exist on disk: {path}")
    return Path(path).read_bytes()


def _image_size(path: str) -> dict:
    try:
        with Image.open(path) as img:
            return {"width": img.width, "height": img.height, "mode": img.mode}
    except Exception as exc:
        return {"error": str(exc)}


def analyze_floorplan_sync(plan_path: str):
    """Run the copied developer analyzer for a local floorplan path."""
    plan_raw = _read_required(plan_path, "CAD/layout reference image")
    return _run_async(analyze_floorplan(plan_raw))


def analyze_floorplan_verified_sync(plan_path: str):
    """Analyze + Gate-1 fidelity loop. Returns (FloorPlan, FidelityReport|None)."""
    plan_raw = _read_required(plan_path, "CAD/layout reference image")
    return _run_async(analyze_floorplan_verified(plan_raw))


def _bbox_size(points: list) -> Optional[str]:
    try:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        if not xs or not ys:
            return None
        return f"{max(xs) - min(xs):.1f} × {max(ys) - min(ys):.1f} m"
    except Exception:
        return None


def _geometry_facts(floorplan_json: Optional[str]) -> str:
    """Distill the extracted FloorPlan JSON into short, countable facts for the
    render prompt. Raw coordinates are useless to an image model, but exact
    room/door/window COUNTS and sizes are precisely what it hallucinates.
    Fails open: any parse problem returns "" and the render proceeds."""
    if not floorplan_json:
        return ""
    try:
        import json

        data = json.loads(floorplan_json)
        rooms = data.get("rooms") or []
        openings = data.get("openings") or []
        doors = sum(1 for o in openings if o.get("type") == "door")
        windows = sum(1 for o in openings if o.get("type") == "window")
        outline = data.get("outline") or [
            p
            for w in (data.get("walls") or [])
            for p in (w.get("start"), w.get("end"))
            if p
        ]
        footprint = _bbox_size(outline)

        room_lines = []
        for r in rooms:
            size = _bbox_size(r.get("polygon") or [])
            name = r.get("name") or "room"
            room_lines.append(f"  - {name}" + (f": {size}" if size else ""))

        facts = [
            "\nEXACT GEOMETRY FACTS (machine-extracted from the CAD plan; "
            "IMAGE 1 was built from exactly these numbers, so your render "
            "must match them too):"
        ]
        if footprint:
            facts.append(f"- Building footprint (bounding box): {footprint}")
        facts.append(f"- Rooms: {len(rooms)}")
        facts.extend(room_lines)
        facts.append(f"- Doors: {doors} | Windows: {windows}")
        facts.append(
            "Before finalizing, COUNT the rooms, doors and windows in your "
            "render — each count must EQUAL the numbers above; no extras, "
            "none missing."
        )
        return "\n".join(facts) + "\n"
    except Exception:
        return ""


def generate_guided_render(
    *,
    project_id: str,
    snapshot_path: str,
    plan_path: str,
    rooms: str = "",
    description: str = "",
    mode: Optional[str] = None,
    style: Optional[str] = None,
    user_style_reference_paths: Optional[list[str]] = None,
    room_overlays: Optional[list[dict]] = None,
    floorplan_json: Optional[str] = None,
    correction_hints: Optional[list[str]] = None,
) -> Generate3DResult:
    """Generate one guided Step 2 render using the developer Gemini ordering."""
    try:
        render_mode = _normalize_mode(mode, style)
        snapshot = _read_required(snapshot_path, "snapshot image")
        plan_raw = _read_required(plan_path, "CAD/layout reference image")
        plan_png = prepare_image(plan_raw).png_bytes

        extra_images: list[tuple[bytes, str]] = [(plan_png, "image/png")]
        has_refs = False

        bundled_ref = bundled_sketchup_reference(render_mode)
        if bundled_ref is not None:
            extra_images.append(bundled_ref)
            has_refs = True

        user_refs = asset_style_references(user_style_reference_paths or [])
        if user_refs:
            extra_images.extend(user_refs)
            has_refs = True

        geometry_facts = _geometry_facts(floorplan_json)
        prompt = build_render_prompt(
            mode=render_mode,
            rooms=rooms,
            style=style or "",
            description=description,
            has_refs=has_refs,
            geometry_facts=geometry_facts,
        )
        # Lean-retry hints: the judge's observed discrepancies are appended
        # (additively - the design prompt above is untouched) so the single
        # regeneration targets the actual defects instead of rerolling blindly.
        if correction_hints:
            hints = "\n".join(f"- {h}" for h in correction_hints[:8])
            prompt += (
                "\n\n[FIDELITY CORRECTIONS - HIGHEST PRIORITY]\n"
                "A previous attempt at this exact render did not match the "
                "layout. It had these specific errors. Do NOT repeat them; "
                "match IMAGE 1's geometry exactly:\n" + hints
            )
        image_bytes = _run_async(
            photorealize(
                snapshot,
                guess_mime(snapshot_path),
                prompt,
                extra_images,
            )
        )

        # Dimension labels are now drawn by Gemini itself (sourcing numbers from
        # the CAD document, IMAGE 2), so the deterministic pixel-overlay is
        # disabled to avoid double labelling.
        overlay_applied_count = 0

        return Generate3DResult(
            render_images=[image_bytes],
            render_metadata={
                "provider": "gemini",
                "provider_label": "Gemini guided render",
                "model": ai_settings.GEMINI_IMAGE_MODEL,
                "source_label": "gemini_guided",
                "mode": render_mode,
                "image_order": [
                    "text_prompt",
                    "snapshot_camera_reference",
                    "cad_layout_reference",
                    "bundled_sketchup_style_reference" if bundled_ref else None,
                    "user_style_references" if user_refs else None,
                ],
                "guided_debug": {
                    "snapshot_path": snapshot_path,
                    "snapshot_size": _image_size(snapshot_path),
                    "snapshot_source": "frontend_three_webgl" if "webgl_snapshot" in Path(snapshot_path).name else "backend_or_external_snapshot",
                    "plan_path": plan_path,
                    "plan_size": _image_size(plan_path),
                    "mode": render_mode,
                    "rooms": rooms,
                    "description_length": len(description or ""),
                    "has_bundled_style_reference": bundled_ref is not None,
                    "user_style_reference_count": len(user_refs),
                    "dimension_overlay_count": overlay_applied_count,
                    "dimension_overlay_input_count": len(room_overlays or []),
                    "geometry_facts_included": bool(geometry_facts),
                },
            },
            success=True,
        )
    except Exception as exc:
        return Generate3DResult(
            success=False,
            error=str(exc),
        )


def generate_guided_render_checked(**kwargs) -> Generate3DResult:
    """Lean render check: ONE generation, ONE fast-judge verify, and at most
    ONE corrective retry on mismatch. This is intentionally NOT the removed
    best-of-N Gate 2 (2 parallel generations + up to 3 slow verifies on every
    request) — the happy path costs a single ~10s judge call.

    Fails open in every direction: check disabled / plan unreadable / judge
    error -> the plain single generation is returned untouched.
    """
    if not ai_settings.RENDER_VERIFY_ENABLED:
        return generate_guided_render(**kwargs)

    try:
        plan_png = prepare_image(
            _read_required(kwargs.get("plan_path"), "CAD/layout reference image")
        ).png_bytes
    except Exception:
        return generate_guided_render(**kwargs)

    def _verify(result: Generate3DResult):
        if not result.success or not result.render_images:
            return None
        try:
            render_png = prepare_image(result.render_images[0]).png_bytes
            return verification.verify_render_sync(plan_png, render_png)
        except Exception as exc:  # fail-open
            return verification.FidelityReport(
                score=0, passed=True, layout_matches=True, error=str(exc)
            )

    def _annotate(result: Generate3DResult, report, attempts: int) -> Generate3DResult:
        if report is None:
            return result
        meta = result.render_metadata or {}
        meta.setdefault("fidelity", {}).update(
            {
                "render_score": report.score,
                "render_passed": report.passed,
                "render_issues": report.issues,
                "render_attempts_used": attempts,
                "render_verify_error": report.error,
            }
        )
        result.render_metadata = meta
        if not report.passed and report.error is None:
            result.warnings = list(result.warnings or []) + [
                f"3D render fidelity below target (score {report.score}); "
                f"best of {attempts} attempt(s)."
            ]
        return result

    first = generate_guided_render(**kwargs)
    report = _verify(first)
    if report is not None:
        print(
            f"🎯 render-verify: score={report.score} passed={report.passed} "
            f"issues={report.issues[:4]}"
        )
    if report is None or report.passed or report.error is not None:
        return _annotate(first, report, attempts=1)

    # One targeted retry, then keep whichever attempt scored higher.
    hints = [i for i in report.issues if isinstance(i, str) and i.strip()]
    second = generate_guided_render(correction_hints=hints or None, **kwargs)
    report2 = _verify(second)
    if report2 is None:
        return first if not second.success else _annotate(second, None, attempts=2)
    print(
        f"🎯 render-verify (retry): score={report2.score} passed={report2.passed} "
        f"issues={report2.issues[:4]}"
    )
    if report2.passed or report2.score >= report.score:
        return _annotate(second, report2, attempts=2)
    return _annotate(first, report, attempts=2)
