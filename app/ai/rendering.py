"""
Agentic Floor Plan 3D Rendering Pipeline — v3 (SINGLE UNIVERSAL NO-REFERENCE STYLE)
===============================================================

PATCH SUMMARY vs previous version:
────────────────────────────────────
1. SINGLE STYLE PATH — NO REFERENCES
   - LIGHTING_ENVIRONMENT now has a mandatory pre-flight checklist block.
   - Enumerates every blob/halo artifact pattern the model must self-check
     and reject before outputting.
   - Stage 2B prompts inject a dedicated "SHADOW SELF-CHECK" block
     immediately before the FINAL REMINDER so the model encounters it last.

2. BASE COLOUR + MATERIAL ENFORCEMENT  (Locked Universal Theme)
   - StyleSpec defaults now match the bright reference image family:
       wall_top #111111 | walls #FFFFFF | floor #EEEEEE
       windows #D7F1F7  | frames #4A4A4A | bathroom #FFFFFF
   - Stage 2B table treats hex values as base/albedo colours and asks for
     clean SketchUp-style materials, glass reflection, crisp bevels,
     and no visible floor shadows instead of dirty realistic darkening.
   - Added COLOUR SELF-CHECK block that model must pass before outputting.

3. SMARTER MEASUREMENT DISPLAY  (Redesigned)
   - _build_measurement_mandate() now produces THREE tiers:
       Tier 1 — Room/area dimensions only (white box, centered, no names)
       Tier 2 — Overall building boundary lines (outside footprint)
       Tier 3 — Wall height label on ONE face + ONE sample door/window
   - No longer puts W=…ft on every individual window edge.
   - Full plans: copy exact numeric dimensions only. Partial: copy + estimate missing.
     None: estimate all + mark (est.).
   - Overall output is compact yet complete.

4. STAGE 1 FURNITURE HARD LOCK  (Stricter)
   - Stage 1 prompt now has a dedicated "FURNITURE SYMBOL PROCESSING"
     section that enforces: grey 2D footprint slabs ONLY for any symbol
     present in the floor plan. Explicitly forbids rendering 3D furniture
     objects, 3D bed shapes, or any non-slab element in Stage 1.

5. PARTIAL FURNITURE MANDATE PRECISION  (Fixed)
   - _build_furniture_mandate() now emits a room-by-room explicit table
     with ZERO ambiguity: each room gets either "FULL SET OF SLABS" or
     "EMPTY — zero objects".
   - Furniture self-check block added inside the mandate.

All public entry-point signatures are preserved for drop-in compatibility.
"""

import os
import glob
import uuid
import json
import base64
import io
import re
import hashlib
from typing import Optional, Tuple, List, Dict, Any
from collections import defaultdict
from PIL import Image as PILImage, ImageChops, ImageFilter, ImageDraw
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import requests
from google import genai
from google.genai import types as google_types
from openai import OpenAI

from app.ai.config import ai_settings
from app.ai.llm_call_logging import log_llm_call


# ═══════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════

class Provider(Enum):
    OPENROUTER    = "openrouter"
    GEMINI_VERTEX = "gemini_vertex"


class AgentAction(Enum):
    GENERATE_FULL       = "generate_full"
    STYLE_UPDATE        = "style_update"
    AREA_EDIT           = "area_edit"
    CONVERSATIONAL_EDIT = "conversational_edit"


# ═══════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class StyleSpec:
    """
    Locked universal colour/material spec.
    No reference image is used anywhere in the active generation path.
    Defaults are pre-set to the approved standard style
    (SketchUp-style: near-black wall tops, bright walls, light grey floor, pale blue windows).
    """
    # -- Mandatory surfaces (approved universal theme) ----------------------
    wall_top_color:      str = "#111111"   # consistent near-black cap
    wall_color:          str = "#FAFAF7"   # clean matte off-white / neutral architectural white
    floor_color:         str = "#E8E6E0"   # light warm-grey microcement / polished concrete
    window_glass_color:  str = "#D7F1F7"   # bright pale blue glass
    window_frame_color:  str = "#4A4A4A"   # dark neutral grey frame
    bathroom_wall_color: str = "#FAFAF7"   # same clean matte off-white
    background_color:    str = "#FFFFFF"   # pure white

    # -- Lighting ----------------------------------------------------------
    lighting_temp_kelvin: str = "6500"
    lighting_description: str = "bright neutral white studio lighting, high exposure, clean SketchUp-style brightness, no warm tint, no beige cast, no yellow cast, no visible shadows"
    floor_material: str = "light warm-grey polished concrete / microcement floor, matte-to-satin finish"
    floor_texture: str = "subtle cloudy low-contrast microcement variation, clean architectural surface, no tile seams, no wood pattern, no marble veins, no speckles, no dirty patches, no noisy shadows"
    wall_material: str = "clean matte off-white architectural wall surface, smooth plain finish, minimal shading, crisp black SketchUp outlines, no wallpaper, no brick, no marble, no decorative texture"
    glass_material: str = "bright pale blue transparent architectural glass, neutral clean tint, subtle reflection, no green cast"
    wood_material: str = "warm wood veneer with subtle grain and realistic roughness"
    fabric_material: str = "clean woven fabric with gentle cushion softness"
    lighting_mood: str = "clean bright architectural render, high ambient light, neutral white illumination, no warm colour pollution, no dark corners, no visible AO"
    render_engine_style: str = "SketchUp/Lumion-style realistic architectural visualisation"

    # -- Furniture palette -------------------------------------------------
    wardrobe_color:         str = "#A07840"   # warm brown wood
    bed_frame_color:        str = "#FFFFFF"   # white platform
    bedding_color:          str = "#D8D8D8"   # light grey smooth fabric
    sofa_color:             str = "#808080"   # mid grey upholstery
    table_color:            str = "#4A3728"   # dark wood
    kitchen_cabinet_color:  str = "#D0D0D0"   # light grey lacquer
    kitchen_counter_color:  str = "#606060"   # dark stone

    extra_notes: str = ""

    def to_prompt_table(self) -> str:
        """
        Render as a numbered material table injected into Stage 2B.
        Hex values are treated as base/albedo colours, not flat paint fills.
        """
        rows = [
            ("01", "WALL TOPS",        self.wall_top_color,
             "PURE NEAR-BLACK #111111 — MUST appear on EVERY wall segment with zero omissions, including short walls, thin partitions, internal walls, enclosed-area walls, and tiny return walls. Razor-sharp SketchUp-style cap, no shading variation allowed"),
            ("02", "WALL FACES",        self.wall_color,
             f"{self.wall_material} — bright off-white, matte, not grey, not beige, not yellow"),
            ("03", "FLOOR SURFACE",     self.floor_color,
             f"{self.floor_material} base colour — {self.floor_texture}; ultra-subtle cloudy material variation is allowed, but no blotchy shadows or noisy grain"),
            ("04", "WINDOW GLASS",      self.window_glass_color,
             f"{self.glass_material} — fill every window cutout, not solid fill"),
            ("05", "WINDOW FRAME",      self.window_frame_color,
             "dark metal/aluminium frame — crisp thin profile, realistic edge shading"),
            ("06", "BATHROOM WALLS",    self.bathroom_wall_color,
             "same clean bright wall surface as regular walls — no tile, no teal, no special treatment"),
            ("07", "BACKGROUND",        self.background_color,
             "clean studio background outside footprint — neutral, unobtrusive, no grey cast"),
            ("08", "WARDROBES",         self.wardrobe_color,
             f"{self.wood_material} — subtle grain, realistic roughness, not flat brown"),
            ("09", "BED FRAMES",        self.bed_frame_color,
             "clean platform material — crisp bevels, hard silhouette, no floor shadow, no AO halo"),
            ("10", "BEDDING",           self.bedding_color,
             f"{self.fabric_material} — gentle folds, not noisy or cartoonish"),
            ("11", "SOFAS",             self.sofa_color,
             f"{self.fabric_material} upholstery — clean soft form, hard silhouette, no floor shadow, no AO halo"),
            ("12", "TABLES",            self.table_color,
             "wood/laminate tabletop — subtle grain or sheen, crisp edges"),
            ("13", "KITCHEN CABINETS",  self.kitchen_cabinet_color,
             "lacquer/laminate finish — crisp bevels, clean panels, no floor shadow, no AO halo"),
            ("14", "KITCHEN COUNTER",   self.kitchen_counter_color,
             "stone/laminate counter — clean surface, crisp edges, no noisy reflection, no shadow spill"),
        ]
        lines = [
            "╔═══════════════════════════════════════════════════════════════════════╗",
            "║  LOCKED BASE COLOUR + MATERIAL TABLE                                  ║",
            "║  Rules:                                                               ║",
            "║    • Use hex values as BASE / ALBEDO colours only.                     ║",
            "║    • Preserve each colour family with clean SketchUp-style materials.  ║",
            "║    • Use no visible floor shadows; keep floor variation nearly zero.  ║",
            "║    • Keep floors smooth: no white speckles or cloudy white patches.   ║",
            "║    • Keep walls/floors bright but not overexposed or pure white.      ║",
            "╠═══╦══════════════════╦═══════════╦══════════════════════════════════╣",
        ]
        for num, surface, colour, note in rows:
            lines.append(f"║{num:>3}║ {surface:<18}║ {colour:<9} ║ {note:<34}║")
        lines.append("╚═══╩══════════════════╩═══════════╩══════════════════════════════════╝")
        if self.lighting_description:
            lines.append(f"\nLIGHTING: {self.lighting_temp_kelvin}K — {self.lighting_description}")
        if self.extra_notes:
            lines.append(f"\nADDITIONAL NOTES FROM REFERENCE:\n{self.extra_notes}")
        lines.append(f"\nTEXTURE TARGET: {self.render_engine_style}; {self.lighting_mood}")
        return "\n".join(lines)


@dataclass
class PipelineResult:
    success:                  bool
    stage:                    str
    # Final PNG bytes. The pipeline hands images back in memory; the Django
    # adapter is what persists them, as a ProjectAsset under media/. The
    # path fields below are kept for callers that still read them and stay
    # None — nothing is written to a pipeline output directory any more.
    image_bytes:              Optional[bytes]     = None
    output_abs_path:          Optional[str]       = None
    output_url_path:          Optional[str]       = None
    base_geometry_abs_path:   Optional[str]       = None
    final_render_abs_path:    Optional[str]       = None
    final_render_url_path:    Optional[str]       = None
    warnings:                 List[str]           = field(default_factory=list)
    metadata:                 Dict                = field(default_factory=dict)
    error:                    Optional[str]       = None
    references_used:          List[Dict]          = field(default_factory=list)
    style_enforcement_log:    List[str]           = field(default_factory=list)


@dataclass
class EditHistoryEntry:
    action:           str
    instruction:      str
    render_abs_path:  str
    render_url_path:  str
    timestamp:        str
    metadata:         Dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════
# SESSION MEMORY
# ═══════════════════════════════════════════════════════════════

class EditSessionMemory:
    MAX_HISTORY = 8

    def __init__(self, persist_dir: Optional[str] = None) -> None:
        self._sessions:    Dict[str, List[EditHistoryEntry]] = defaultdict(list)
        self._persist_dir = persist_dir
        if persist_dir:
            os.makedirs(persist_dir, exist_ok=True)
            self._load_all()

    def _key(self, floor_plan_url: str) -> str:
        return hashlib.md5(floor_plan_url.encode()).hexdigest()[:16]

    def _persist_path(self, key: str) -> Optional[str]:
        if not self._persist_dir:
            return None
        return os.path.join(self._persist_dir, f"session_{key}.json")

    def _load_all(self) -> None:
        if not self._persist_dir:
            return
        for path in glob.glob(os.path.join(self._persist_dir, "session_*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
                key = os.path.basename(path)[8:-5]
                self._sessions[key] = [EditHistoryEntry(**e) for e in data]
            except Exception:
                pass

    def _save(self, key: str) -> None:
        path = self._persist_path(key)
        if not path:
            return
        try:
            with open(path, "w") as f:
                json.dump([vars(e) for e in self._sessions[key]], f, indent=2)
        except Exception:
            pass

    def record(self, floor_plan_url: str, entry: EditHistoryEntry) -> None:
        key = self._key(floor_plan_url)
        self._sessions[key].append(entry)
        if len(self._sessions[key]) > self.MAX_HISTORY:
            self._sessions[key].pop(0)
        self._save(key)

    def history(self, floor_plan_url: str) -> List[EditHistoryEntry]:
        return list(self._sessions.get(self._key(floor_plan_url), []))

    def last_render(self, floor_plan_url: str) -> Optional[EditHistoryEntry]:
        h = self.history(floor_plan_url)
        return h[-1] if h else None

    def history_summary(self, floor_plan_url: str) -> str:
        h = self.history(floor_plan_url)
        if not h:
            return "EDIT HISTORY: None — this is the first modification."
        lines = [f"  • [{e.timestamp}] {e.action.upper()}: {e.instruction!r}" for e in h]
        return "EDIT HISTORY (already applied — do NOT undo):\n" + "\n".join(lines)

    def clear(self, floor_plan_url: str) -> None:
        key = self._key(floor_plan_url)
        self._sessions.pop(key, None)
        path = self._persist_path(key)
        if path and os.path.exists(path):
            os.remove(path)


# ═══════════════════════════════════════════════════════════════
# CANVAS PROCESSOR
# ═══════════════════════════════════════════════════════════════

class CanvasProcessor:
    TARGET_FILL      = 0.76
    ABS_MIN_PAD_PX   = 15
    MIN_PAD_RATIO    = 0.025
    BG_TOLERANCE     = 22
    WHITE_TOLERANCE  = 12
    MIN_CONTENT_DIM  = 80
    BORDER_STRIP_PX  = 4

    @classmethod
    def _strip_border_artifacts(cls, img: PILImage.Image) -> PILImage.Image:
        w, h = img.size
        p = cls.BORDER_STRIP_PX
        if w <= p * 2 + 10 or h <= p * 2 + 10:
            return img
        return img.crop((p, p, w - p, h - p))

    @classmethod
    def _sample_background(cls, img: PILImage.Image) -> Tuple[int, int, int]:
        w, h = img.size
        pts = [
            (2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3),
            (w // 2, 2), (2, h // 2), (w - 3, h // 2), (w // 2, h - 3),
        ]
        samples = [img.getpixel((x, y)) for x, y in pts if 0 <= x < w and 0 <= y < h]
        if not samples:
            return (255, 255, 255)
        return tuple(sum(c[i] for c in samples) // len(samples) for i in range(3))

    @classmethod
    def _clean_background(cls, img: PILImage.Image) -> PILImage.Image:
        bg = cls._sample_background(img)
        if all(abs(bg[i] - 255) <= cls.WHITE_TOLERANCE for i in range(3)):
            return img
        bg_img       = PILImage.new("RGB", img.size, bg)
        diff         = ImageChops.difference(img, bg_img).convert("L")
        content_mask = diff.point(lambda px: 255 if px > cls.BG_TOLERANCE else 0)
        white_img    = PILImage.new("RGB", img.size, (255, 255, 255))
        return PILImage.composite(img, white_img, content_mask)

    @classmethod
    def add_safe_canvas(cls, image: PILImage.Image) -> PILImage.Image:
        img = image.convert("RGB")
        img = cls._strip_border_artifacts(img)
        img = cls._clean_background(img)

        white_ref = PILImage.new("RGB", img.size, (255, 255, 255))
        diff      = ImageChops.difference(img, white_ref).convert("L")
        binary    = diff.point(lambda px: 255 if px > cls.WHITE_TOLERANCE else 0)
        bbox      = binary.getbbox()

        if not bbox:
            return img

        x1, y1, x2, y2 = bbox
        cw, ch = x2 - x1, y2 - y1

        if cw < cls.MIN_CONTENT_DIM or ch < cls.MIN_CONTENT_DIM:
            return img

        pad_ratio     = (1.0 / cls.TARGET_FILL - 1.0) / 2.0
        pad_from_fill = max(int(cw * pad_ratio), int(ch * pad_ratio))
        pad_aesthetic = max(int(max(cw, ch) * cls.MIN_PAD_RATIO), cls.ABS_MIN_PAD_PX)
        pad = max(pad_from_fill, pad_aesthetic)

        canvas  = PILImage.new("RGB", (cw + 2 * pad, ch + 2 * pad), (255, 255, 255))
        content = img.crop((x1, y1, x2, y2))
        canvas.paste(content, (pad, pad))
        return canvas

    @classmethod
    def prepare_input(cls, image: PILImage.Image, max_dim: int = 2048) -> PILImage.Image:
        img = image.convert("RGB")
        w, h = img.size
        if min(w, h) < 512:
            scale = 512 / min(w, h)
            img = img.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)
        return img


# ═══════════════════════════════════════════════════════════════
# PROMPTS
# ═══════════════════════════════════════════════════════════════

class Prompts:

    # ───────────────────────────────────────────────────────────
    # STAGE 1 — GEOMETRY ANCHOR  (v2: strict furniture slab rule)
    # ───────────────────────────────────────────────────────────

    STAGE_1_GEOMETRY = """
        OUTPUT QUALITY STANDARD — MANDATORY:
        This output will be used as a professional architectural deliverable.
        Every line must be ruler-straight. Every angle must be exact.
        Walls must have clean, sharp, high-contrast edges.
        Result must look like professional CAD/SketchUp software output.

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        ★ CAMERA — THE SINGLE MOST IMPORTANT RULE ★
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        YOU MUST PRODUCE A 3D MODEL — NOT A 2D PLAN.

        WHAT CORRECT LOOKS LIKE (pass = output this):
          ✓ You can clearly see wall HEIGHT on TWO exterior faces
          ✓ Camera is a true 3D isometric/axonometric view, not a near-vertical plan view
          ✓ Floor occupies roughly 50–65% of the canvas (not 80–90%)
          ✓ Wall faces are visible as rectangles, not thin lines
          ✓ The result looks like a SketchUp 3D model photo
          ✓ Interior of rooms visible through the open roof

        WHAT WRONG LOOKS LIKE (fail = REGENERATE, never output):
          ✗ Walls appear as thin outlines only — no visible height
          ✗ Floor fills 80% or more of the canvas
          ✗ Camera is too top-down or reads like a flat 2D plan
          ✗ The result looks like a 2D plan view with slight shading
          ✗ Rooms look like flat coloured rectangles on paper
          ✗ No visible exterior wall face at all

        ╔══════════════════════════════════════════════════════╗
        ║  FLAT-VIEW SELF-TEST — run this BEFORE outputting:  ║
        ║  Q1: Can you see wall HEIGHT on ≥ 2 exterior faces? ║
        ║      YES → continue | NO → REGENERATE               ║
        ║  Q2: Does the floor fill < 70% of canvas area?      ║
        ║      YES → continue | NO → REGENERATE (too flat)    ║
        ║  Q3: Do walls have clearly visible vertical faces?  ║
        ║      YES → continue | NO → REGENERATE               ║
        ║  Q4: Is the camera strongly 3D/isometric, not flat? ║
        ║      YES → continue | NO → REGENERATE               ║
        ║  ALL FOUR MUST BE YES. Any NO = regenerate.         ║
        ╚══════════════════════════════════════════════════════╝

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        [STAGE 1/2: GEOMETRY ANCHOR — FULL FIDELITY LIFT]
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        Convert this 2D floor plan into a clean white-box 3D model.
        Preserve geometry and numeric measurement/dimension annotations only. Do NOT preserve room/function labels such as BEDROOM, KITCHEN, AUDITORIUM, LOBBY, HALL, LOUNGE, CARPORCH, CAR PARK, TOILET, etc.

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        GEOMETRY ACCURACY CHECKLIST
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        □ 1. Room COUNT matches floor plan exactly.
        □ 2. Every internal partition wall is present — including thin ones.
        □ 3. No wall moved, merged, or simplified.
        □ 4. Building footprint shape matches floor plan exactly.
        □ 5. Door openings.
        □ 6. Window cutouts in EVERY exterior wall where they exist.
        □ 7. Staircase rendered with visible step risers (if present).
        □ 8. Model NOT stretched or compressed horizontally.
        □ 9. No element closer than 10% to any canvas edge.
        □ 10. Camera passes the FLAT-VIEW SELF-TEST above.
        If any box fails — REGENERATE. Do NOT output a failing render.

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        CANVAS PLACEMENT
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        • Model occupies NO MORE than 72% of canvas area.
        • Equal white margins: Top ≥ 13%, Bottom ≥ 13%, Left ≥ 13%, Right ≥ 13%.
        • Every dimension line, numeric dimension tag, and measurement callout: FULLY INSIDE canvas.
        • NO element closer than 10% to any canvas edge.

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        TEXT / LABEL POLICY — DIMENSIONS ONLY
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        • Remove all room/function name labels from the 3D model.
        • Do NOT write labels such as ROOM, SMALL ROOM, AREA, BAR AREA,
          SERVICE AREA, CORRIDOR, STAIR, STAIRS, BED ROOM, BEDROOM, KITCHEN,
          AUDITORIUM, LOBBY, HALL, LOUNGE, CARPORCH, CAR PARK, BATHROOM,
          TOILET, STORE, OFFICE, LAWN, TERRACE, BALCONY, PORCH, TOTAL,
          OVERALL, WIDTH, DEPTH, etc.
        • Do NOT preserve title blocks, legends, north arrows, notes, or textual names.
        • Preserve/generate ONLY numeric architectural dimensions:
          room/area L×W values, wall dimensions, overall footprint dimensions,
          wall height, wall thickness, door/window width callouts.
        • If original text is a pure numeric dimension (e.g. 12'-0", 3.50 m,
          10×12, W=3'-0"), keep it. If it is a room/function name, remove it.
        • If an enclosed/usable area has NO written size in the original plan,
          add an estimated numeric L×W size in the same unit system and mark it (est.).
          This applies to every visually separated enclosed or usable region,
          including small edge regions and narrow regions.

        ╔══════════════════════════════════════════════════════════════╗
        ║  OCR-STYLE TEXT FILTER — APPLY BEFORE DRAWING ANY TEXT       ║
        ╠══════════════════════════════════════════════════════════════╣
        ║  The source floor plan often has two-line room labels like:  ║
        ║      BED ROOM                                                ║
        ║      9'-0" × 13'-0"                                         ║
        ║  In the 3D result this MUST become ONLY:                     ║
        ║      9'-0" × 13'-0"                                         ║
        ║  Delete the first/text name line completely.                 ║
        ║                                                              ║
        ║  FOR EVERY text cluster in the source image:                 ║
        ║    1) Split into separate lines.                             ║
        ║    2) Keep ONLY lines containing numbers + dimension units    ║
        ║       or dimension separators: ', ft, in, m, mm, ×, x, W=, H=║
        ║    3) Delete lines containing only words or room functions.   ║
        ║    4) Never re-type deleted words anywhere in the image.      ║
        ║                                                              ║
        ║  HARD BAN — these tokens may not appear anywhere:            ║
        ║  ROOM, SMALL ROOM, AREA, BAR AREA, SERVICE AREA, CORRIDOR,   ║
        ║  STAIR, STAIRS, BED, BEDROOM, BED ROOM, KITCHEN, HALL,       ║
        ║  TOILET, BATH, BATHROOM, BALCONY, AUDITORIUM, LOBBY,         ║
        ║  LOUNGE, CARPORCH, CAR PARK, PORCH, LAWN, TERRACE, OFFICE,   ║
        ║  STORE, FIRST FLOOR, GROUND FLOOR, FLOOR PLAN, PLAN, TOTAL,  ║
        ║  OVERALL, WIDTH, DEPTH.                                      ║
        ║                                                              ║
        ║  If a dimension is tied to a banned word, keep the numeric    ║
        ║  part only and discard the word.                              ║
        ╚══════════════════════════════════════════════════════════════╝

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        STYLE RULES (Stage 1 — white-box only)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        • Walls   : PURE NEUTRAL WHITE (#FFFFFF) — flat clean fill, zero texture, no beige/yellow tint
        • Floors  : VERY LIGHT NEUTRAL GREY (#EEEEEE) — flat matte, smooth and clean, zero blotches
        • NO shadows, NO ambient occlusion, NO materials, NO gradients
        • Clean black outline edges on wall tops only
        - Every wall edge, partition line, and building corner must have a
        razor-sharp 5px solid black stroke outline (#0A0A0A).
        - Apply stroke to: all exterior wall edges, all interior partition edges,
        window frame perimeters, door frame perimeters, staircase step edges.
        - Stroke must be continuous — no gaps, no fading, no anti-alias blur.
        - This is a hard CAD-style linework pass — like SketchUp's "edges ON" mode.
        • Solid pure white background (#FFFFFF)
        WALL TOP CAP COVERAGE — ABSOLUTE RULE:
        • EVERY wall must have a visible near-black top cap.
        • Do NOT omit wall-top caps on short wall pieces, internal partitions,
          narrow return walls, step-adjacent walls, enclosed-area walls, or tiny segments.
        • If a wall exists, it must show the dark top cap.
        • No wall may appear with only white side faces and no top cap.
        - SHARPNESS MANDATE — Stage 1 is a HARD-EDGE CAD model:
            Every wall edge, window frame, door frame, and staircase step
            must be rendered with maximum geometric precision.
            Anti-aliasing is FORBIDDEN on structural edges.
            Think: SketchUp model screenshot with edges ON, profiles ON,
            no post-processing blur, no soft rendering pass.
            Output must look like it was drawn with a ruler and set square.

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        GEOMETRY RULES
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        • Trace EVERY wall exactly as positioned — no simplification
        • Maintain 100% aspect ratio — DO NOT stretch
        • All walls at strict 90-degree orthogonal angles
        • Preserve ALL internal partition walls including thin ones
        • Generate actual 3D doors: detect door positions from the original 2D floor plan
        and place hinged 3D door models at those exact positions. Each door should be
        slightly open (20–35 degrees) so the doorway is visually recognizable. Attach
        each door panel to the correct wall edge.

        ╔══════════════════════════════════════════════════════════════╗
        ║  DOOR ARC LINES — ABSOLUTE ZERO TOLERANCE                    ║
        ║  THIS APPLIES TO ALL SPACE TYPES INCLUDING COMMERCIAL        ║
        ╠══════════════════════════════════════════════════════════════╣
        ║  The 2D floor plan shows curved quarter-circle arc lines     ║
        ║  to indicate door swing direction. These are 2D drafting     ║
        ║  symbols ONLY. They DO NOT EXIST in a 3D model.             ║
        ║                                                              ║
        ║  STRICTLY FORBIDDEN — zero exceptions:                       ║
        ║    ✗ Any curved arc line drawn on the floor                  ║
        ║    ✗ Any quarter-circle sweep line at a doorway              ║
        ║    ✗ Any thin curved line near any door opening              ║
        ║    ✗ Any door swing indicator of any kind on the floor       ║
        ║    ✗ Any semicircle, arc, or curved floor marking            ║
        ║                                                              ║
        ║  THIS INCLUDES:                                              ║
        ║    ✗ Cinema/auditorium entry doors                           ║
        ║    ✗ Emergency exit doors                                    ║
        ║    ✗ Any entry, exit, interior, or service door               ║
        ║    ✗ Every single door in the plan — no exceptions           ║
        ║                                                              ║
        ║  WHAT A DOOR LOOKS LIKE IN 3D (the ONLY acceptable form):   ║
        ║    ✓ A flat rectangular door panel attached to wall edge     ║
        ║    ✓ Door panel rotated 20–35 degrees open                   ║
        ║    ✓ Clean floor surface beneath — no lines, no curves       ║
        ║                                                              ║
        ║  MANDATORY SELF-CHECK before outputting:                     ║
        ║    Scan EVERY doorway in the entire building.                ║
        ║    Count the doors. Check each one individually.             ║
        ║    If even ONE curved line exists on the floor → REMOVE      ║
        ║    ALL arc lines and regenerate. Zero tolerance.             ║
        ╚══════════════════════════════════════════════════════════════╝
        Windows as rectangular cutouts filled with #D7F1F7 bright pale blue glass.
        Add thin #4A4A4A frame border around each pane.
          DO NOT leave windows empty or white.
        • Render staircase with visible step risers if present


        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        ╔══════════════════════════════════════════════════════════════╗
        ║  FURNITURE SYMBOL PROCESSING — CRITICAL STAGE 1 RULE        ║
        ╠══════════════════════════════════════════════════════════════╣
        ║  Stage 1 renders GEOMETRY ONLY.                              ║
        ║  ANY furniture/fixture symbol in the floor plan is           ║
        ║  rendered as a FLAT 2D GREY SLAB on the floor — NOTHING MORE.║
        ╠══════════════════════════════════════════════════════════════╣
        ║  STRICT SLAB RULES:                                          ║
        ║    ✓ Slab height = 1 px (effectively flat/2D on floor)       ║
        ║    ✓ Slab fill = #C0C0C0 (medium grey — flat, no texture)    ║
        ║    ✓ Slab footprint = EXACT match to floor plan symbol size  ║
        ║    ✓ Slab position = EXACT match to floor plan symbol pos    ║
        ║
        ╠══════════════════════════════════════════════════════════════╣
        ║  STRICTLY FORBIDDEN IN STAGE 1:                              ║
        ║    ✗ 3D bed shapes, mattress volumes, headboards             ║
        ║    ✗ 3D sofa shapes or cushion volumes                       ║
        ║    ✗ 3D wardrobe / cabinet volumes                           ║
        ║    ✗ Any object with height > 1px                            ║
        ║    ✗ Any furniture NOT drawn in the floor plan               ║
        ║    ✗ Inference: "BED ROOM" ≠ add a bed slab unless drawn    ║
        ╠══════════════════════════════════════════════════════════════╣
        ║  IF ZERO furniture symbols exist in the floor plan:          ║
        ║    Every floor surface MUST be 100% EMPTY. Absolutely zero  ║
        ║    objects of any kind. Stage 2 will decide what to add.    ║
        ╚══════════════════════════════════════════════════════════════╝


        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        OUTER BUILDING DIMENSIONS
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        Place thin black dimension lines with tick endpoints OUTSIDE the
        building footprint (not overlapping any wall).
        Label total Width on top + bottom; total Depth on left + right.
        Keep all dimension lines within the canvas boundary.

        TITLE BLOCK / ROOM TEXT: do NOT reproduce title blocks, floor titles, room/function labels, or any word-only text. Only numeric dimensions and measurement callouts are allowed. Example: 'FIRST FLOOR PLAN' must be deleted; '50'' may remain.
    """

    # ───────────────────────────────────────────────────────────
    # STAGE 2A-1 — ANALYZE FLOOR PLAN
    # ───────────────────────────────────────────────────────────

    STAGE_2A_ANALYZE_FLOORPLAN = """
        You are an expert architectural analyst. Carefully study this 2D floor plan.

        Return ONLY a JSON object — no markdown fences, no explanation:
        {
          "space_type": "<residential_house | residential_apartment | office | retail_shop | restaurant | cafe | cinema | hotel | clinic | gym | warehouse | mixed_use>",
          "subtype_notes": "<e.g. '3-bedroom apartment'>",
          "key_rooms": ["<room1>", "<room2>", "..."],
          "scale": "<small | medium | large>",
          "style_recommendation": "<modern_residential | retail_commercial>",
          "has_furniture": "<true | false>",
          "furniture_coverage": "<full | partial | none>",
          "furniture_by_room": {"<room_name>": ["<item1>", "<item2>"], "...": []},
          "has_dimensions": "<true | false>",
          "dimension_coverage": "<full | partial | none>",
          "furniture_items": ["<item1>", "..."],
          "areas_with_dimensions": ["<area/room where numeric LxW or dimension is already written>", "..."],
          "areas_missing_dimensions": ["<area/room/balcony/corridor/stair/porch/car park/usable zone with no numeric LxW written>", "..."],
          "all_enclosed_or_usable_areas": ["<every room/area/zone including balconies, stairs, passages, porches, car parks>", "..."],
          "unit_system": "<metric | imperial | unknown>",
          "dominant_unit": "<m | mm | ft | in | unknown>",
          "wall_height_estimate": "<e.g. '9ft-0in'>",
          "wall_thickness_ext": "<e.g. '9in'>",
          "wall_thickness_int": "<e.g. '4.5in'>",
          "door_width_typical": "<e.g. '3ft-0in'>",
          "window_width_typical": "<e.g. '3ft-0in'>"
        }

        Definitions:
        • "has_furniture"      : true ONLY if actual drawn furniture/fixture SYMBOLS or OUTLINES
                                 exist as graphical elements in the floor plan image.
                                 A room NAME like "BED ROOM", "KITCHEN", "Zimmer", "Schlafzimmer"
                                 is NOT furniture. Text labels are NEVER furniture.
                                 Only count items that are DRAWN as shapes/outlines
                                 (e.g. a rectangle with rounded corners = bed symbol,
                                  circles = toilet/sink, hatched rectangle = wardrobe).
        • "furniture_coverage" : full = nearly all rooms have drawn symbols; partial = some rooms;
                                 none = zero drawn symbols (only text labels exist).
        • "furniture_by_room"  : ONLY list rooms where actual drawn symbols exist.
                                 DO NOT list rooms just because their name implies furniture.
                                 e.g. if "BED ROOM" has no drawn bed symbol → do NOT list it.
        • "has_dimensions"     : true ONLY if explicit numeric values (m, mm, ft, ") appear.
        • "dimension_coverage" : full = nearly all rooms/areas labelled; partial = some; none = zero.
        • "areas_with_dimensions" : include only zones that already have a visible numeric size/dimension.
        • "areas_missing_dimensions" : include EVERY usable/enclosed zone that lacks a numeric L×W size,
                                  including balcony, corridor, passage, porch, car porch, car park, stair,
                                  landing, lobby, terrace, lawn, utility, store, shaft/open area if present.
                                  If a small enclosed/usable region has no visible numeric L×W size,
                                  it MUST be listed here.
        • "all_enclosed_or_usable_areas" : complete coverage list. Count EVERY visually separated
                                  enclosed or usable region, including very small rooms, tiny pockets,
                                  stair landings, narrow passages, service spaces, edge areas, entry pockets,
                                  irregular niches, balconies, porches, shafts/open usable zones,
                                  and partial-width spaces. Do not skip an area because it is small or hard to name.
        • "unit_system"        : detect from labels. None → "metric" if European, "imperial" if US-style.
        • "dominant_unit"      : unit used most. Guess from context if not labelled.
        • wall/door/window estimates: estimate from scale bar or proportion if not explicit.

        CRITICAL: Be extremely conservative with has_furniture. Default to false if unsure.
        A floor plan with only room name labels and wall lines has NO furniture.
        Only return has_furniture=true if you can see actual drawn object symbols.
    """

    # ───────────────────────────────────────────────────────────
    # STAGE 2A-2 — DISABLED LEGACY STYLE EXTRACTION PROMPT
    # ───────────────────────────────────────────────────────────

    STAGE_2A_EXTRACT_STYLE = """
        DISABLED LEGACY PROMPT. Do not use this prompt in the active pipeline.
        This pipeline uses the fixed universal no-reference style only.

        You are an expert architectural visualisation material analyst.
        Study this reference render image VERY carefully.
        Extract base surface colours, material behaviour, texture feel, lighting, and render-engine style.

        Return ONLY a JSON object — no markdown fences, no explanation:
        {
          "wall_top_color": "<hex e.g. #1A1A1A>",
          "wall_color": "<hex>",
          "floor_color": "<hex>",
          "floor_material": "<clean smooth architectural floor | hardwood | marble | carpet>",
          "floor_texture": "<brief description of smoothness, evenness, and noise control>",
          "wall_material": "<brief description of pure neutral white matte wall behaviour with no tint or broad variation>",
          "window_glass_color": "<hex>",
          "glass_material": "<brief description of tint, transparency, reflection>",
          "window_frame_color": "<hex>",
          "bathroom_wall_color": "<hex — use wall_color value, do NOT extract teal/coloured tile>",
          "background_color": "<hex>",
          "lighting_temp_kelvin": "<number>",
          "lighting_description": "<brief>",
          "lighting_mood": "<brief description of shadows, AO, contrast, ambience>",
          "render_engine_style": "<e.g. SketchUp/Lumion realistic architectural render>",
          "wardrobe_color": "<hex>",
          "wood_material": "<brief description of grain, veneer, roughness>",
          "bed_frame_color": "<hex>",
          "bedding_color": "<hex>",
          "fabric_material": "<brief description of weave, softness, wrinkles>",
          "sofa_color": "<hex>",
          "table_color": "<hex>",
          "kitchen_cabinet_color": "<hex>",
          "kitchen_counter_color": "<hex>",
          "extra_notes": "<1-3 sentences on distinctive style features>"
        }

        Rules:
        • Sample the dominant base/albedo colour for each surface directly from image pixels.
          DO NOT flatten the style into solid fills; preserve material and lighting cues.
        • Extract visible texture/material qualities: clean wall smoothness, light floor finish,
          glass reflection/transparency, wood/fabric grain, clean edge treatment, bevel treatment.
        • If a surface type is NOT visible: use architectural defaults:
            wall_top: #111111 | walls: #FFFFFF | floor: #EEEEEE
            window_glass: #D7F1F7 | frame: #4A4A4A | bathroom: #FFFFFF
            background: #FFFFFF
        • Hex values: exactly 7 chars — # + 6 UPPERCASE hex digits.
    """

    # ───────────────────────────────────────────────────────────
    # GEOMETRY LOCK HEADER (all Stage 2B prompts)
    # ───────────────────────────────────────────────────────────

    _GEOMETRY_LOCK_HEADER = """
╠════════════════════════════════════════════════════════════════════╣
║  STEP 1 — GEOMETRY LOCK (layout only)                              ║
╠════════════════════════════════════════════════════════════════════╣
║  From IMAGE 1 and original floor plan, preserve EXACTLY:          ║
║    Room count, relative positions, wall layout, footprint shape   ║
║    Building rotation direction, door/window positions             ║
║    Numeric dimension values/callouts only                         ║
║  DO NOT preserve any word-only text from IMAGE 1.                 ║
║  FORBIDDEN: moving walls, changing rooms, rotating building       ║
╠════════════════════════════════════════════════════════════════════╣
║  TEXT FILTER — NUMBERS ONLY                                       ║
╠════════════════════════════════════════════════════════════════════╣
║  Before output, scan all visible text in IMAGE 1.                  ║
║  Keep ONLY numeric dimension strings: examples 9'-0" × 13'-0",    ║
║  3.50 × 3.00 m, 50', 45', W = 3ft-0in, H = 9ft-0in.               ║
║  Delete every word/function/title token even when it is next to    ║
║  a dimension. Example: 'BED ROOM 9'-0" × 13'-0"' becomes only      ║
║  '9'-0" × 13'-0"'.                                                ║
║  Never write: ROOM, SMALL ROOM, AREA, BAR AREA, SERVICE AREA,     ║
║  CORRIDOR, STAIR, STAIRS, BEDROOM, BED ROOM, KITCHEN, HALL,       ║
║  TOILET, BATHROOM, AUDITORIUM, LOBBY, LOUNGE, CARPORCH, CAR PARK, ║
║  BALCONY, PORCH, LAWN, FLOOR PLAN, FIRST FLOOR PLAN,              ║
║  GROUND FLOOR PLAN, OFFICE, TOTAL, OVERALL, WIDTH, DEPTH.         ║
║  Allowed visible text only: numeric dimensions and measurement     ║
║  callouts. Any other text = failed render.                         ║
╚════════════════════════════════════════════════════════════════════╝
"""

    # ───────────────────────────────────────────────────────────
    # ★★★  ANTI-SHADOW-BLOB BLOCK  ★★★
    # Injected at the END of every Stage 2B prompt so it's the
    # LAST thing the model reads before generating output.
    # ───────────────────────────────────────────────────────────

    _ANTI_SHADOW_BLOB_FINAL_CHECK = """
    ╔══════════════════════════════════════════════════════════════╗
    ║  ★  FINAL RENDER SELF-CHECK — MANDATORY BEFORE OUTPUT  ★     ║
    ╠══════════════════════════════════════════════════════════════╣
    ║  REJECT AND REGENERATE if any are visible:                   ║
    ║                                                              ║
    ║    ✗ Pixelated or blocky shadows                             ║
    ║    ✗ Speckled / noisy floor texture                          ║
    ║    ✗ White scattered pixels around dimension text             ║
    ║    ✗ Broken, blurry, glowing, or partially erased dimension text║
    ║    ✗ Cloud-like grey/white floor patches                     ║
    ║    ✗ Dirty ambient-occlusion smudges                          ║
    ║    ✗ Radial halos around walls, windows, doors, or dimension text║
    ║    ✗ Large dark patches cast by walls across floors           ║
    ║    ✗ Curved door swing arc lines on the floor (ANY door,     ║
    ║      ANY room type — residential OR commercial)              ║
    ║                                                              ║
    ║  REQUIRED OUTPUT:                                            ║
    ║    ✓ Wall tops remain accurate near-black caps                ║
    ║    ✓ Every wall/partition edge has a crisp 5px dark stroke  ║
    ║    ✓ No edge is soft, faded, or dissolved into material       ║
    ║    ✓ Wall faces look bright, clean, and almost flat           ║
    ║    ✓ Floor remains clean smooth light grey architectural floor║
    ║    ✓ Dimension text is crisp black CAD-style overlay text      ║
    ║    ✓ Texture never crosses over or damages dimension text      ║
    ║    ✓ No visible ambient occlusion or floor shadow scatter     ║
    ║    ✓ Every material boundary is razor-sharp — no blurred edges      ║
    ║    ✓ Glass colour fully contained within frame — zero floor bleed   ║
    ╚══════════════════════════════════════════════════════════════╝
    """

    _STRICT_EDIT_SCOPE_LOCK = """
╔══════════════════════════════════════════════════════════════════════╗
║  EDIT SCOPE LOCK — ABSOLUTE RULE                                    ║
╠══════════════════════════════════════════════════════════════════════╣
║  You are editing an existing 3D render, NOT regenerating a plan.     ║
║  Understand the user's intended result as a whole, then make the     ║
║  smallest coherent edit that achieves it. Unspecified content is     ║
║  locked; do not add optional improvements or creative extras.        ║
╠══════════════════════════════════════════════════════════════════════╣
║  USER INSTRUCTION:                                                   ║
║  {instruction}                                                       ║
╠══════════════════════════════════════════════════════════════════════╣
║  STRICT RULES:                                                       ║
║    1. Apply the requested semantic change, not brittle word-by-word  ║
║       substitutions.                                                 ║
║    2. Allow only unavoidable local integration needed for that edit  ║
║       (correct overlap, occlusion, edge, and immediate contact).      ║
║    3. If scope is ambiguous, choose the least invasive reasonable    ║
║       interpretation; do not infer extra improvements.               ║
║    4. Do not redesign the scene.                                     ║
║    5. Do not restyle materials, colors, lighting, or objects unless  ║
║       explicitly requested.                                          ║
║    6. Do not regenerate the whole image from scratch if a local edit ║
║       is enough.                                                     ║
║    7. Preserve the current 3D render as-is except for the requested  ║
║       change.                                                        ║
╠══════════════════════════════════════════════════════════════════════╣
║  PRESERVE EXACTLY UNLESS USER EXPLICITLY REQUESTS OTHERWISE:         ║
║    • overall layout, room positions, wall positions, wall thickness  ║
║    • camera angle, perspective, crop, framing, scale                 ║
║    • floor material and floor color                                  ║
║    • wall colors, toilet/bathroom wall colors, wall top caps         ║
║    • window glass color and window frame style                       ║
║    • door/window positions, stairs, openings                         ║
║    • dimension annotations already visible                           ║
║    • lighting style, brightness, exposure, shadow behavior           ║
║    • existing furniture and textures/material theme                  ║
║    • sharp outlines / SketchUp edge clarity                          ║
╠══════════════════════════════════════════════════════════════════════╣
║  SOURCE-APPEARANCE + ARTIFACT LOCK:                                  ║
║    • Copy every untargeted surface from IMAGE 1; do not repaint,      ║
║      retexture, clean up, enhance, or relight it.                     ║
║    • A continuous floor/wall/material must keep the same colour,      ║
║      texture scale, grain direction, exposure, and coverage on both  ║
║      sides of the edit boundary unless that surface is the target.   ║
║    • For a targeted material change, cover only the requested scope   ║
║      with one stable, continuous material—never a patchy rerender.    ║
║    • Never introduce white/transparent/erased holes, washed-out       ║
║      islands, broken texture, speckles, stippling, salt-and-pepper    ║
║      noise, checkerboarding, or random bright/dark patches.           ║
║    • Do not "repair" a source area outside the requested edit.       ║
╠══════════════════════════════════════════════════════════════════════╣
║  TEXT / LABEL PROTECTION:                                            ║
║    ✗ Do NOT add room labels.                                         ║
║    ✗ Do NOT add function labels.                                     ║
║    ✗ Do NOT add title blocks.                                        ║
║    ✗ Do NOT add legends.                                             ║
║    ✗ Do NOT add random text.                                         ║
║    ✗ Do NOT restore 2D plan annotations.                             ║
║    ✗ Do NOT add labels unless user explicitly asked for labels.      ║
╠══════════════════════════════════════════════════════════════════════╣
║  NO-UNREQUESTED-CHANGE EXAMPLES:                                     ║
║    • If user says "add furniture", ONLY add furniture.               ║
║      Do NOT change toilet wall colors, floor, walls, windows,        ║
║      camera, labels, dimensions, or lighting.                        ║
║    • If user says "change floor", ONLY change floor.                 ║
║      Do NOT change walls, windows, toilet colors, furniture, labels. ║
║    • If user says "add sofa", ONLY add that sofa.                    ║
║      Do NOT add extra decor or other furniture.                      ║
║    • If user says "make walls darker", ONLY change walls.            ║
║      Do NOT change floor, windows, furniture, labels, camera.        ║
╠══════════════════════════════════════════════════════════════════════╣
║  FINAL SELF-CHECK BEFORE OUTPUT:                                     ║
║    □ Did I apply only the requested instruction(s)?                  ║
║    □ Did I avoid changing unrelated colors/materials?                ║
║    □ Did I preserve toilet/bathroom wall colors unless requested?    ║
║    □ Did I preserve floor/walls/windows/camera unless requested?     ║
║    □ Are all surfaces continuous with zero new white/noisy patches?  ║
║    □ Did I avoid adding labels/text?                                 ║
║  If any answer is NO, regenerate and fix before output.              ║
╚══════════════════════════════════════════════════════════════════════╝
    """


    # ───────────────────────────────────────────────────────────
    # FURNITURE MANDATES
    # ───────────────────────────────────────────────────────────

    _ZERO_FURNITURE_MANDATE = """
╔══════════════════════════════════════════════════════════════════╗
║  ██  FURNITURE POLICY: ZERO FURNITURE — ABSOLUTE RULE  ██        ║
╠══════════════════════════════════════════════════════════════════╣
║  The original floor plan contains NO drawn furniture symbols.    ║
║  Therefore every floor surface in EVERY room = 100% EMPTY.      ║
╠══════════════════════════════════════════════════════════════════╣
║  ★★★  ROOM NAMES ARE NOT FURNITURE INSTRUCTIONS  ★★★             ║
║                                                                  ║
║  These room labels do NOT authorise adding ANYTHING:             ║
║    "BED ROOM" / "BEDROOM" / "Schlafzimmer"                      ║
║       → do NOT add: bed, wardrobe, nightstand, or any item      ║
║    "KITCHEN" / "Küche"                                          ║
║       → do NOT add: cabinets, counters, appliances, sink        ║
║    "Zimmer" / "HALL" / "LIVING ROOM" / "Wohnzimmer"             ║
║       → do NOT add: sofas, tables, chairs, or any furniture     ║
║    "TOILET" / "BAD" / "BATHROOM"                                ║
║       → do NOT add: toilet, sink, bathtub, or any fixture       ║
║    "BAR" / "LOUNGE" / "AUDITORIUM"                              ║
║       → do NOT add: seats, stools, bar counter, or any element  ║
║    ANY label name whatsoever                                     ║
║       → do NOT add ANY object based on what the name implies    ║
╠══════════════════════════════════════════════════════════════════╣
║  THE ONLY VALID REASON to render an object in a room:            ║
║    A physical drawn symbol for that object existed as a          ║
║    graphical element in the ORIGINAL FLOOR PLAN IMAGE.           ║
║    No drawn symbol in original = zero objects in that room.      ║
╠══════════════════════════════════════════════════════════════════╣
║  COMPLETELY FORBIDDEN in every room:                             ║
║    ✗ Beds, mattresses, pillows, headboards                      ║
║    ✗ Sofas, armchairs, chairs, stools, benches                  ║
║    ✗ Tables, desks, counters, islands, bar counters             ║
║    ✗ Wardrobes, closets, shelves, cabinets of any kind          ║
║    ✗ Kitchen appliances or kitchen fittings of any kind         ║
║    ✗ Bathroom fixtures (toilet, sink, tub, shower, mirror)      ║
║    ✗ Cinema/theatre seats or auditorium rows                    ║
║    ✗ Rugs, curtains, plants, lamps, art, decorations            ║
║    ✗ Any object, prop, or accessory whatsoever                  ║
╠══════════════════════════════════════════════════════════════════╣
║  SELF-CHECK before outputting — if ANY fails, remove the item:  ║
║    □ Every room floor is completely empty (only floor colour)?   ║
║    □ Zero objects added based on a room name?                    ║
║    □ Zero objects added by inference from space type?            ║
╚══════════════════════════════════════════════════════════════════╝
"""


    _FURNITURE_MIRRORING_MANDATE = """
╔══════════════════════════════════════════════════════════════╗
║  ⚠  FURNITURE POLICY: FULL MIRRORING                        ║
║  Replace each grey placeholder slab with the matching piece. ║
╠══════════════════════════════════════════════════════════════╣
║  RULES:                                                      ║
║    ✓ Replace EACH grey slab with correct furniture piece     ║
║    ✓ Match slab POSITION and FOOTPRINT SIZE exactly          ║
║    ✓ Apply colours from LOCKED STYLE SPEC above              ║
║    ✗ DO NOT add furniture outside placeholder positions      ║
║    ✗ DO NOT invent furniture not in the plan                 ║
╠══════════════════════════════════════════════════════════════╣
║  Furniture detected in source plan:                          ║
║  {furniture_list}
║                                                              ║
║  FURNITURE SELF-CHECK:                                       ║
║    □ Every slab replaced with correct piece?                 ║
║    □ No furniture added outside slab positions?              ║
║    □ All furniture colours match the STYLE SPEC?             ║
║    □ No specular noise or rough texture on furniture?        ║
╚══════════════════════════════════════════════════════════════╝
"""

    _PARTIAL_FURNITURE_MANDATE = """
╔══════════════════════════════════════════════════════════════╗
║  ⚠  FURNITURE POLICY: GREY-SLAB-SPECIFIC (partial)          ║
╠══════════════════════════════════════════════════════════════╣
║  For each internal numeric entry below, the rule is ABSOLUTE:║
╠══════════════╦═══════════════════════════════════════════════╣
║    ID         ║  ACTION                                       ║
╠══════════════╬═══════════════════════════════════════════════╣
{room_table}╠══════════════╩═══════════════════════════════════════════════╣
║  HARD RULES:                                                 ║
║    ✓ FURNISHED entries: replace grey slabs at exact positions║
║    ✓ Apply colours from LOCKED STYLE SPEC                    ║
║    ✗ EMPTY entries: zero objects — NONE — not even a rug     ║
║    ✗ DO NOT infer any object from text or space type         ║
╠══════════════════════════════════════════════════════════════╣
║  FURNITURE SELF-CHECK:                                       ║
║    □ Every FURNISHED entry has its slabs replaced?           ║
║    □ Every EMPTY entry has zero objects on the floor?        ║
║    □ No furniture added outside grey slab positions?         ║
╚══════════════════════════════════════════════════════════════╝
"""

    # ───────────────────────────────────────────────────────────
    # LIGHTING ENVIRONMENT  (v2: anti-blob rules hardened)
    # ───────────────────────────────────────────────────────────

    LIGHTING_ENVIRONMENT = """
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        RENDER QUALITY TARGET
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        Target look: clean SketchUp-style architectural visualisation.
        Bright, sharp, clean, minimal, and noise-free.
        NOT moody. NOT shadow-heavy. NOT dirty. NOT grainy.

        LIGHTING:
          • Bright high-key neutral white daylight — 6500K.
          • Every room fully and evenly lit like a clean SketchUp viewport export.
          • White surfaces must remain neutral white — not ivory, not cream, not beige, not yellow.
          • Floor receives maximum diffuse light.
          • No room darker than 95% of maximum brightness.
          • Shadows: ZERO visible shadows across the floor.
          • The only allowed dark mark at wall bases is a crisp 1px floor/wall edge stroke, not a shadow.

        ╔══════════════════════════════════════════════════════════════╗
        ║  SHADOW / CLEAN FLOOR RULES — ABSOLUTE ZERO TOLERANCE       ║
        ╠══════════════════════════════════════════════════════════════╣
        ║  ALLOWED:                                                   ║
        ║    ✓ Only a crisp 5px floor/wall edge stroke exactly on the boundary     ║
        ║    ✓ No ambient occlusion, no soft contact shadow, and no shadow spread beyond the wall edge║
        ║                                                             ║
        ║  FORBIDDEN:                                                 ║
        ║    ✗ Broad soft shadows across open floor areas             ║
        ║    ✗ Speckled floor noise                                   ║
        ║    ✗ Grainy concrete texture                                ║
        ║    ✗ Cloudy grey/white floor blobs                          ║
        ║    ✗ Dirty AO smudges                                       ║
        ║    ✗ Random scratches, stains, grunge, dust, mottling       ║
        ║    ✗ White sparkle-like scatter on the floor                ║
        ║    ✗ Uneven bright/dark floor patches                       ║
        ║    ✗ Salt-and-pepper shadow noise                           ║
        ║    ✗ Stippled grey dots                                     ║
        ║    ✗ Scattered grey pixels                                  ║
        ║    ✗ Granular shadow texture                                ║
        ║    ✗ Shadow dust around furniture or walls                  ║
        ║    ✗ White noisy islands on the floor                       ║
        ║    ✗ Grey halo around walls or seating rows                 ║
        ║    ✗ Beige cast on walls                                    ║
        ║    ✗ Cream/ivory tint on white surfaces                     ║
        ║    ✗ Yellowish light pollution on floors or walls           ║
        ║    ✗ Warm brownish overall colour grading                   ║
        ╚══════════════════════════════════════════════════════════════╝

        FLOOR MATERIAL RULE:
          Default floor must stay light warm-grey polished concrete / microcement.
          It should have a matte-to-satin architectural finish with subtle cloudy
          low-contrast material variation, like the reference floor.
          Do NOT make it plain pure white, dark concrete, wood, marble, tile grid, carpet,
          or glossy stone unless the user explicitly asks.
          No noisy concrete grain, no visible particles, no speckling, no dirty patches,
          no cloudy SHADOW shapes, no salt-and-pepper noise, no grey stippling,
          and no scattered pixels.

        SHARPNESS + LINEWORK:
          • Every wall edge must remain crisp and dark.
          • Use sharp 5px dark stroke on visible wall edges.
          • No blurry boundaries.
          • No glow.
          • No feathered shadow edges.
          • Linework must remain clearly visible on top of materials.

        WINDOWS:
          • Fill EVERY window opening with the glass colour from STYLE SPEC.
          • Add thin frame border per STYLE SPEC.
          • NO empty windows. NO white windows.
    """


    # ───────────────────────────────────────────────────────────
    # STAGE 2B — WITH REFERENCE (v2: colour table hardened,
    #            shadow check injected at end)
    # ───────────────────────────────────────────────────────────

    STAGE_2B_APPLY_STYLE = (
        _GEOMETRY_LOCK_HEADER
        + """
        [STAGE 2/2: STYLE COMPOSITOR — Reference-guided]

        You receive:
          IMAGE 1 — 3D white-box skeleton (GEOMETRY LOCK — see header above).
          IMAGE 2 — Reference render (visual atmosphere guide only).
                    The LOCKED STYLE SPEC table below is the authoritative colour source.

        Space context:
          • Space type : {space_type}
          • Subtype    : {subtype_notes}
          • Count      : use IMAGE 1 visually; do not write any area names.

        ════════════════════════════════════════════════════════════
        LOCKED STYLE SPECIFICATION — AUTHORITATIVE COLOUR SOURCE
        (Extracted from reference — override EVERYTHING with these values)
        ════════════════════════════════════════════════════════════
        {style_spec_table}
        ════════════════════════════════════════════════════════════
        CLEAN RENDER LOCK — MUST MATCH TARGET OUTPUT
        ════════════════════════════════════════════════════════════
        The final render must stay extremely clean, bright, and sharp.
        • Remove broad shadows and dirty shading.
        • Floor must be smooth, flat, very light grey, and visually even.
        • Walls must stay pure neutral white.
        • No cloudy floor patches.
        • No dirty corner darkening.
        • No visible ambient occlusion except the tiniest wall-base grounding.
        • Overall look must be a clean SketchUp export, not a realistic moody render.
        • Strokes/edges must stay sharp and clearly visible.

        ════════════════════════════════════════════════════════════
        WALL WHITENESS LOCK:
        • All wall faces must be pure neutral white #FFFFFF.
        • No cream, beige, ivory, warm grey, or yellow tint.
        • Do not apply plaster color variation on wall faces.
        • Use only thin edge shadows; do not darken broad wall surfaces.
        • Wall tops remain #111111 and must not bleed onto white wall faces.

        ════════════════════════════════════════════════════════════
        FLOOR + SHADOW CLEANUP LOCK:
        • Floor must appear smooth, clean, and neutral light grey.
        • Commercial and residential outputs must use the exact same clean SketchUp style.
        • Shadows must be visually absent from open floor areas.
        • Do not create broad grey shading across open floor areas.
        • Do not create cloudy shadow patches, scattered noise, grey stippling, salt-and-pepper dots, or dirty corner darkening.
        • Keep the result bright, clean, and SketchUp-like.

        ════════════════════════════════════════════════════════════
        FLOOR UNIFORMITY LOCK — MATCH APPROVED GOOD OUTPUTS
        ════════════════════════════════════════════════════════════
        • Floor must be one continuous clean #EEEEEE-style surface.
        • Do not create local bright islands, white clouds, grey clouds, or erased-looking patches.
        • Do not brighten the floor around dimension text, labels, furniture, beds, tables, chairs, sofas, counters, or fixtures.
        • Do not create halos around furniture or text.
        • Do not create grey dust around wall bases or object bases.
        • Large open floor areas must remain smooth and even.
        • Target result style: stable clean SketchUp output, smooth floor, no scattered shadow/noise.

        ════════════════════════════════════════════════════════════
        WALL TOP + FLOOR LOCK:
        • Every wall segment must show the near-black top cap with zero omissions.
        • This includes all small walls, partitions, enclosed-area walls, and narrow returns.
        • Floor must appear very light neutral grey and visually even.
        • Do not let the floor drift darker, dirtier, or more textured than the locked floor colour.

        ════════════════════════════════════════════════════════════
        WALL TOP SOURCE-OVERRIDE LOCK — CRITICAL
        ════════════════════════════════════════════════════════════
        • Do NOT inherit wall-top colour from the original floor plan.
        • Do NOT inherit missing/white/grey wall tops from IMAGE 1.
        • Regardless of what the source image shows, repaint EVERY wall-top face to #111111.
        • This applies to exterior walls, interior partitions, short walls, thin walls,
          enclosed-area walls, stair-adjacent walls, tiny returns, and narrow wall pieces.
        • If the source floor plan has no black wall caps, still create black wall-top caps.
        • If any wall segment has white/grey/missing top cap, the render is incorrect.
        • Wall-top rule is stronger than source-image appearance.

        ════════════════════════════════════════════════════════════
        BRIGHTNESS LOCK — MUST MATCH REFERENCE
        ════════════════════════════════════════════════════════════
        The final render must stay bright, clean, and high-key.
        • Keep surfaces bright but not overexposed.
        • Walls must appear neutral white with slight readable surface definition, never cream, beige, yellow, grey, or blown out.
        • Floor must appear smooth continuous neutral light grey, never pure white or dark concrete.
        • Window glass must appear consistently bright pale blue, not dull, white, or dark.
        • Interior corners must remain clean and bright.
        • Use very minimal shading only.
        • No white speckles, no blown-out patches, no cloudy white floor areas.
        • Avoid moody realism, dirty ambient occlusion, grey haze, or broad shadows.
        • Overall look must be closer to a clean SketchUp export than a realistic shaded render.

        ════════════════════════════════════════════════════════════
        CAMERA DEPTH LOCK — AVOID FLAT TOP-DOWN OUTPUT
        ════════════════════════════════════════════════════════════
        The camera must read as a true 3D isometric/axonometric SketchUp view.
        • Wall side faces must be clearly visible as vertical rectangles.
        • Show wall height on at least two exterior sides.
        • Do not output a near-vertical 2D plan with slight wall height.
        • If the floor dominates the image and wall faces look thin, regenerate with a lower isometric camera angle.

        ════════════════════════════════════════════════════════════
        REFERENCE THEME COLOUR LOCK — DO NOT DRIFT
        ════════════════════════════════════════════════════════════
        Match the LEFT reference image theme:
        • Wall faces must stay pure neutral white #FFFFFF.
        • Wall top caps must stay crisp black/graphite #111111.
        • Floor must stay very light neutral grey #EEEEEE, not warm grey, brown-grey, pure white, or dark concrete.
        • Window glass must be bright pale blue #D7F1F7, not white, grey, or green teal.
        • Window frames must be dark grey #4A4A4A.
        • Background must remain pure white #FFFFFF.
        • Overall look: clean SketchUp architectural render, not moody realistic render.
        • Do not add grey haze, beige cast, yellow cast, dirty ambient occlusion, dark shadows, or concrete mottling.

        ════════════════════════════════════════════════════════════
        TEXTURE / MATERIAL TARGET
        ════════════════════════════════════════════════════════════
        Use IMAGE 2 as the visual texture reference.
        Match its clean bright SketchUp-style architectural render feel:
        • clean bright wall surfaces with slight readable definition and no grey falloff
        • smooth continuous light grey floor with no speckles, no white scatter, and no cloudy patches
        • black wall caps with sharp edges
        • blue glass panels with transparency and reflection
        • realistic counters, wood, fabric, metal, and tile materials where present
        • only the faintest contact grounding, no dark shadows
        • keep colours from Style Spec, with clean controlled SketchUp brightness, not blown-out exposure

        ════════════════════════════════════════════════════════════
        ★ GLOBAL FURNITURE RULE — READ BEFORE EVERYTHING ELSE ★
        ════════════════════════════════════════════════════════════
        Furniture and objects are determined SOLELY by drawn symbols
        in the ORIGINAL FLOOR PLAN — never by room label names.

        A room called "BED ROOM" does NOT mean add a bed.
        A room called "KITCHEN" does NOT mean add counters or appliances.
        A room called "BAD" or "TOILET" does NOT mean add fixtures.
        A room called "Zimmer", "Hall", "Bar", "Auditorium" does NOT
        mean add any furniture, seats, or elements.

        The ONLY thing that authorises adding an object is:
        a drawn graphical symbol for that object existed in the
        original 2D floor plan. No drawn symbol = no object.

        ════════════════════════════════════════════════════════════
        FURNITURE POLICY
        ════════════════════════════════════════════════════════════
{furniture_mandate}

        ════════════════════════════════════════════════════════════
        DIMENSION ANNOTATIONS
        ════════════════════════════════════════════════════════════
        {measurement_mandate}

        MICRO-AREA MEASUREMENT LOCK:
        • Every separated enclosed/usable region must have one numeric L×W measurement.
        • Small spaces are not optional.
        • Do not skip tiny rooms, narrow corridors, service spaces, stair pockets,
          edge areas, entries, porches, balconies, or irregular niches.
        • If a dimension cannot fit inside, place it nearby with a short leader line.
        • Final output is incomplete if any usable/enclosed region has no numeric size.

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        WALLS & SURFACES
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        • Apply colours from LOCKED STYLE SPEC as base/albedo colours.
        • Render surfaces as clean bright architectural materials, not dirty realistic fills.
        • Wall side faces must remain PURE NEUTRAL WHITE (#FFFFFF).
        • Do not turn wall faces grey, cream, beige, ivory, yellow, or warm off-white.
        • Only allow very faint edge shading for depth.
        • Large wall faces must stay visibly white and clean.
        • Keep EVERY wall position EXACTLY as in IMAGE 1.
        • Wall tops: preserve near-black colour family, thick and razor-sharp with crisp edges.

        COLOUR NEUTRALITY LOCK:
        • Walls must remain neutral white, not cream, beige, ivory, or yellow.
        • Floor must remain neutral light grey, not warm grey or brown-grey.
        • The overall render must feel clean white and cool-neutral, not warm-toned.

        EDGE LINEWORK — MANDATORY:
        - Every wall edge (top cap edge, side face edge, base edge) must carry
        a crisp 5px solid dark stroke (#111111).
        - Interior partition walls: 5px dark stroke on all visible edges.
        - Window cutout perimeter: 3px dark frame stroke.
        - Floor/wall junction lines: visible 1px dark baseline.
        - Wall-top caps must be continuous and complete across the entire building.
        - Do not break, fade, or skip the dark top cap on any wall segment.
        - Linework must remain clearly visible above all material shading.
        - No edge may be soft, blurred, dissolved into material, or missing.
        - Output should look like SketchUp with crisp profiles and edges turned on.
        - Glass/window boundaries: pale blue glass colour HARD-STOPS at frame edge —
        zero colour bleed or feathering onto adjacent wall or floor surface.
        - Furniture silhouettes: every piece has a sharp clean outline —
        no soft glow, no blurred boundary between object and floor.
        - Floor surface: sharp clean plane — no soft blending at wall bases
        beyond the allowed crisp 5px floor/wall edge stroke.
        - Wall top caps: razor-sharp upper and lower edges —
        near-black colour contained exactly within cap geometry.
        - SHARPNESS SELF-CHECK: zoom into any wall corner in your output.
        If edges look soft, blurred, or feathered → REGENERATE with
        harder edge treatment. Target: SketchUp screenshot sharpness.

        ╔══════════════════════════════════════════════════════════════╗
        ║  DOOR ARC LINES — FORBIDDEN IN FINAL RENDER                  ║
        ╠══════════════════════════════════════════════════════════════╣
        ║  If IMAGE 1 contains any curved arc lines on the floor       ║
        ║  near doorways — DO NOT reproduce them in your output.       ║
        ║  These are 2D drafting artifacts that must be erased.        ║
        ║                                                              ║
        ║  When rendering each doorway:                                ║
        ║    ✓ Show only the 3D door panel (slightly open, 20–35°)     ║
        ║    ✓ Floor beneath the doorway = clean, flat, arc-free       ║
        ║    ✗ No curved line on the floor at any door                 ║
        ║    ✗ No quarter-circle marking of any kind                   ║
        ║  This applies to EVERY door in every space type without      ║
        ║  exception.                                                  ║
        ╚══════════════════════════════════════════════════════════════╝

        WALL REALISM:
        • Wall tops are locked: keep exact thickness, shape, and near-black color.
        • Do not repaint, soften, distort, or break wall top caps.
        • Every wall segment must show the dark top cap.
        • No omissions are allowed on short walls, narrow walls, partitions,
          enclosed-area walls, step-adjacent walls, or tiny wall returns.
        • If any wall top is missing, the render is incorrect and must be regenerated.
        • Wall side faces must stay clean pure white.
        • Use only the minimum shading needed to show wall depth.
        • If a wall face looks beige, cream, grey, or yellow, regenerate.
        • No dark flat grey wall fill.
        • No dirty grunge, cracks, stains, or heavy texture.
        • Clean and high-key — like a bright professional SketchUp export.

        FLOORING:
        • Use floor colour as the base colour and keep it visually clean and even.
        • Render the floor as a simple very light neutral-grey architectural surface.
        • Use only extremely subtle shading.
        • Do NOT add concrete roughness, visible grain, or noisy material texture.
        • Do NOT add speckles, dust, scratches, stains, mottling, white scatter, patchy highlights, or cloudy shadow patches.
        • Keep the floor smooth and continuous across all rooms.
        • Do not render visible shadows on the floor; only a crisp 5px floor/wall edge stroke may exist.
        • Floor must be one continuous clean tone — no scattered dots, no grey stippling, no noisy shadow texture.
        • Keep the floor clean under and around all dimension text.
        • Dimension text is an annotation overlay and must remain crisp and unaffected.

        {lighting_environment}


    """
        + _ANTI_SHADOW_BLOB_FINAL_CHECK
    )

    # ───────────────────────────────────────────────────────────
    # STAGE 2B — NO REFERENCE (preset-guided)
    # ───────────────────────────────────────────────────────────

    STAGE_2B_APPLY_STYLE_NO_REF = (
        _GEOMETRY_LOCK_HEADER
        + """
        [STAGE 2/2: SINGLE UNIVERSAL STYLE COMPOSITOR — No reference images]

        IMAGE 1 — 3D white-box skeleton (GEOMETRY LOCK — see header above).

        Space context:
          • Space type : {space_type}
          • Subtype    : {subtype_notes}
          • Count      : use IMAGE 1 visually; do not write any area names.
          • Style      : {style_recommendation}

        ════════════════════════════════════════════════════════════
        INITIAL USER PROMPT / INSTRUCTION ROUTER — MUST OBEY
        ════════════════════════════════════════════════════════════
{user_instruction_block}

        ════════════════════════════════════════════════════════════
        UNIVERSAL COLOUR THEME LOCK — SAME FOR ALL FLOOR PLANS
        ════════════════════════════════════════════════════════════
        • Use the exact same colour theme for every floor plan type.
        • Residential, commercial, cinema, office, restaurant, cafe, clinic, hotel,
          warehouse, mixed-use — all must use this same theme.
        • Wall tops: #111111.
        • Wall faces: #FAFAF7 by default, unless the user explicitly requests a different wall colour/material.
        • Floor: #E8E6E0 light warm-grey polished concrete / microcement by default, unless the user explicitly requests a different floor.
        • Window glass: #D7F1F7.
        • Window frames: #4A4A4A.
        • Background: #FFFFFF.
        • Do not create separate commercial colours or residential colours.

        ════════════════════════════════════════════════════════════
        WALL TOP SOURCE-OVERRIDE LOCK — CRITICAL
        ════════════════════════════════════════════════════════════
        • Do NOT inherit wall-top colour from the original floor plan.
        • Do NOT inherit missing/white/grey wall tops from IMAGE 1.
        • Regardless of what the source image shows, repaint EVERY wall-top face to #111111.
        • This applies to exterior walls, interior partitions, short walls, thin walls,
          enclosed-area walls, stair-adjacent walls, tiny returns, and narrow wall pieces.
        • If the source floor plan has no black wall caps, still create black wall-top caps.
        • If any wall segment has white/grey/missing top cap, the render is incorrect.
        • Wall-top rule is stronger than source-image appearance.

        ════════════════════════════════════════════════════════════
        ★ GLOBAL FURNITURE RULE — READ BEFORE EVERYTHING ELSE ★
        ════════════════════════════════════════════════════════════
        Furniture and objects are determined SOLELY by drawn symbols
        in the ORIGINAL FLOOR PLAN — never by room label names.

        A room called "BED ROOM" does NOT mean add a bed.
        A room called "KITCHEN" does NOT mean add counters or appliances.
        A room called "BAD" or "TOILET" does NOT mean add fixtures.
        A room called "Zimmer", "Hall", "Bar", "Auditorium" does NOT
        mean add any furniture, seats, or elements.

        The ONLY thing that authorises adding an object is:
        a drawn graphical symbol for that object existed in the
        original 2D floor plan. No drawn symbol = no object.

        EXCEPTION — EXPLICIT INITIAL USER INSTRUCTION:
        If the INITIAL USER PROMPT above explicitly asks to add furniture, fixtures,
        objects, or decor, then add ONLY those requested items. Do not infer extra
        objects from room names. Do not change floors, walls, toilets, windows,
        colours, labels, camera, or layout while adding those requested items.

{furniture_mandate}

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        DIMENSION ANNOTATIONS
        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
        {measurement_mandate}

        MICRO-AREA MEASUREMENT LOCK:
        • Every separated enclosed/usable region must have one numeric L×W measurement.
        • Small spaces are not optional.
        • Do not skip tiny rooms, narrow corridors, service spaces, stair pockets,
          edge areas, entries, porches, balconies, or irregular niches.
        • If a dimension cannot fit inside, place it nearby with a short leader line.
        • Final output is incomplete if any usable/enclosed region has no numeric size.

        ╔══════════════════════════════════════════════════════════════╗
        ║  VISUAL STYLE TARGET                                         ║
        ╚══════════════════════════════════════════════════════════════╝
        {style_descriptor}

        THEME CONSISTENCY RULE:
        • The style descriptor is mandatory, not optional.
        • Do not create a different palette based on floor plan type.
        • Do not let commercial/cinema plans become darker, greyer, warmer, or more realistic.
        • Use the same clean SketchUp theme as residential outputs.
        • Wall-top #111111, wall #FAFAF7, floor #E8E6E0 microcement, windows #D7F1F7, frames #4A4A4A by default.

{lighting_environment}


    """
        + _ANTI_SHADOW_BLOB_FINAL_CHECK
    )

    # ───────────────────────────────────────────────────────────
    # STYLE PRESETS (fallback when no reference image)
    # ───────────────────────────────────────────────────────────

    RESIDENTIAL_SKETCHUP_STYLE = """
        VISUAL STYLE — UNIVERSAL CLEAN SKETCHUP STYLE:

        WALL TOPS    : #111111  — crisp near-black cap on every wall segment.
        WALL FACES   : #FAFAF7  — clean matte off-white / neutral architectural white, no cream/beige/yellow cast.
        FLOORS       : #E8E6E0  — light warm-grey polished concrete / microcement, subtle cloudy low-contrast texture, matte/satin finish, no tile seams.
        WINDOWS      : #D7F1F7  — bright pale blue transparent glass.
        Frame: #4A4A4A dark neutral grey aluminium, crisp thin profile.
        BATHROOM WALLS:#FFFFFF  — same neutral white as all other walls.
        BACKGROUND   : #FFFFFF  — pure white.

        SHARPNESS    : SketchUp-style hard edges throughout. Every wall, window
           frame, furniture piece, and floor boundary = razor-sharp.
           No soft rendering, no blurred edges, no glow effects.
           Glass pale blue colour MUST NOT bleed onto floor or walls.
           Output must look like a crisp SketchUp model screenshot.

        Furniture: walnut #A07840 wardrobes, grey #808080 sofas,
        white #FFFFFF bed frames, grey #D8D8D8 bedding, dark #4A3728 tables.
        Grey #D0D0D0 kitchen cabinets. Dark grey #606060 kitchen counter.

        LIGHTING: 6500K bright neutral white studio light, high exposure, minimal shading, no warm tint, no visible shadow.
        Every surface evenly lit. Floor stays near its light warm-grey microcement base and never becomes pure white.
        Shadow rule: NO visible shadow fields, NO shadow clouds, NO scattered shadow noise.
        Only a mathematically thin wall-base contact line may exist, and it must not spread onto open floor.
        No shadow pools. No dark corners. No moody atmosphere. No visible AO.
        No white speckles, no cloudy white floor patches, no grey stippling, no blown-out wall/floor areas.
        No beige cast, cream tint, yellow tint, or warm brown colour grading.
    """

    STYLE_PRESETS = {
        "modern_residential": RESIDENTIAL_SKETCHUP_STYLE,
        "retail_commercial": RESIDENTIAL_SKETCHUP_STYLE,
    }

    # ───────────────────────────────────────────────────────────
    # AREA EDIT
    # ───────────────────────────────────────────────────────────

    STAGE_EDIT_AREA = """
        [TARGETED AREA EDIT — STRICT 3D-ONLY LOCAL EDIT]

        ★★★ EDIT SOURCE LOCK — CRITICAL ★★★
        You are editing ONLY the provided latest/selected 3D render image.
        The original 2D floor plan is NOT provided and must NOT influence this edit.
        Do NOT restore, re-read, infer, or recreate anything from any original 2D floor plan.
        Do NOT add old room labels, function labels, title blocks, plan text, legends, or 2D annotations.

        TWO IMAGES ONLY:
          IMAGE 1 — CLEAN CURRENT 3D RENDER (base — no guide marks).
          IMAGE 2 — CYAN REGION GUIDE derived from IMAGE 1: a cyan outline with a
          light cyan tint marks the edit zone (edit zone only — do NOT reproduce in output).

        USER INSTRUCTION: {instruction}

        {strict_edit_scope_lock}

        ════════════════════════════════════════════════
        READ THE ZONE BEFORE YOU EDIT IT
        ════════════════════════════════════════════════
        The cyan boundary is a ROUGH HUMAN GESTURE — someone quickly drew a
        circle or box around what they meant. It is NOT a precise selection and
        it is NOT a cut line.
        • First identify WHAT is actually inside that zone in IMAGE 1 — which
          room, which surfaces, which furniture and fixtures. The tint is light
          specifically so you can see them; read them.
        • Then work out what the USER INSTRUCTION means FOR THOSE THINGS, and
          apply it to them as a competent designer would.
        • Treat the objects inside the zone as WHOLE objects. If the boundary
          clips through a table, sofa, wall or fixture, edit that whole object —
          never render it half-changed, half-old, and never slice an object
          along the boundary. Nothing may be left cut, truncated or unfinished.
        • Anything the instruction does not concern stays exactly as it is,
          even inside the zone.
        • Keep the edit's own footprint inside the marked zone; do not spread
          the change into neighbouring rooms or across the whole render.

        ════════════════════════════════════════════════
        AREA EDIT TASK
        ════════════════════════════════════════════════
        1. Use IMAGE 1 as the only architectural truth.
        2. Identify the edit zone from IMAGE 2's cyan outline/tint.
        3. Apply USER INSTRUCTION to the real content inside that zone.
        4. OUTSIDE area = pixel-perfect copy of IMAGE 1. Zero changes.
        5. Output: zero cyan guide marks, zero annotation artifacts.

        HARD PRESERVATION LOCK:
        • Same camera angle, crop, scale, perspective, and lighting as IMAGE 1.
        • Same walls, wall positions, partitions, openings, windows, doors, stairs, dimensions, and linework.
        • Same floor/wall theme unless the user explicitly asks to change floor/walls.
        • Do NOT change toilet/bathroom colors, washroom wall colors, windows, wall colors, floor material, labels, dimensions, or unrelated rooms.
        • Do NOT add room names, function labels, legend text, title text, or any new text.
        • Do NOT move the edit into a wrong room or wrong file/image. The cyan region from IMAGE 2 is the only target zone.

        WHAT YOU CAN CHANGE:
        • Only what the user requested.
        • Only inside traced/cyan area.
        • Only as much as needed for the requested edit.

        OUTPUT: clean professional render. Zero region-guide marks. No labels. No unrelated changes.
    """

    # ───────────────────────────────────────────────────────────
    # CHAT EDITS
    # ───────────────────────────────────────────────────────────

    STAGE_CHAT_EDIT_WITH_HISTORY = """
        [CHAT-BASED INSTRUCTION EDIT — STRICT 3D-ONLY EDIT]

        ★★★ EDIT SOURCE LOCK — CRITICAL ★★★
        You are editing ONLY IMAGE 1, the latest/selected 3D render.
        The original 2D floor plan is NOT provided and must NOT influence this edit.
        Do NOT restore, re-read, infer, or recreate anything from the original 2D floor plan.
        Do NOT add old room labels, function labels, title blocks, legends, plan text, or 2D annotations.

        IMAGE 1 — Current/latest 3D render (the ONLY editing base and architectural truth).

        ★★★ PROJECTION / CAMERA LOCK — CRITICAL ★★★
        This is an IN-PLACE edit of IMAGE 1's pixels, NOT a re-render of the building.
        Return IMAGE 1's own viewpoint: identical camera, projection, rotation, tilt,
        zoom, crop, centring, framing and aspect ratio.
        FORBIDDEN — these are rejected outputs:
          ✗ Rotating / orbiting / tilting the scene to a new angle
          ✗ Converting a straight-down, plan-aligned view into an isometric,
            3-quarter, diamond or corner-on view (or the reverse)
          ✗ Re-framing, re-centring, zooming, cropping or changing the aspect ratio
          ✗ Re-drawing the building from scratch "in the same style"
        If IMAGE 1's outer walls run parallel to the image edges, they MUST still run
        parallel to the image edges in your output, with the building in the same place
        in the frame at the same size. Even a broad instruction ("all furniture",
        "everything") is still an in-place swap of the items where they already stand.

        ★★★ ANNOTATION LOCK — CRITICAL ★★★
        IMAGE 1's dimension lines, arrowheads, tick marks and measurement numbers are
        already correct and are NOT part of any edit. Copy them across pixel-for-pixel:
        same values, same wording, same abbreviations, same positions, same font, same
        style, same thickness.
        FORBIDDEN — these are rejected outputs:
          ✗ Re-lettering, re-typesetting or re-positioning any existing measurement
          ✗ Recomputing, rounding, re-deriving or inventing any measurement
          ✗ Re-wording a callout (e.g. "8.5 m" must NOT become "TOTAL WIDTH: 8.5 M")
          ✗ Adding room-name / function labels (KITCHEN, BEDROOM, BATH, …)
          ✗ Adding per-room size callouts that IMAGE 1 does not already have
          ✗ Adding a scale bar, ruler, legend, title block or north arrow
          ✗ Dropping any dimension line or number IMAGE 1 does have

        ★★★ RENDER-QUALITY LOCK — CRITICAL ★★★
        IMAGE 1 also defines the output's visual quality. Reproduce it exactly:
          • Same material detail — floor plank grain, tile joints, fabric weave, metal
            and glass finish. Materials must stay physically detailed, not flat fills.
          • Same sharpness, edge definition and crisp outlines.
          • Same lighting, exposure, contrast, shadow softness and background tone.
          • Same level of realism and the same render style overall.
        FORBIDDEN: smoothing, softening, blurring, flattening, simplifying, stylising,
        re-lighting, or making the render look cartoonish, glossy, plastic or CGI-toy-like.
        Losing IMAGE 1's texture and detail is a rejected output even if the requested
        edit itself is correct.

        SESSION CONTEXT (already applied — do NOT undo):
        {edit_history}

        CURRENT USER INSTRUCTION:
        {instruction}

        {strict_edit_scope_lock}

        ════════════════════════════════════════════════
        CHAT EDIT TASK
        ════════════════════════════════════════════════
        • IMAGE 1 already contains all previous successful changes.
        • Apply ONLY the current user instruction on top of IMAGE 1.
        • Do NOT undo previous edits unless the current instruction explicitly asks.
        • Do NOT add changes not asked for.
        • Current instruction beats prior history only if directly conflicting.
        • If the user asks to add furniture/functions/objects, add only those requested objects.
        • Do NOT change toilet/bathroom/washroom wall colors, floor color/material, wall theme, windows, lighting, camera, dimensions, or unrelated rooms unless explicitly requested.
        • Preserve existing numeric dimensions if visible, but do not create any new room/function labels.
        • No labels, no room names, no title text, no legend text, no 2D floor-plan annotations.
        • Do not place the requested change in the wrong image/file; edit IMAGE 1 only.

        OUTPUT: Clean professional 3D render. Same camera angle as IMAGE 1. No labels. No unrelated changes.
    """

    STAGE_CHAT_EDIT = """
        [CHAT-BASED INSTRUCTION EDIT — STRICT 3D-ONLY]

        ★★★ EDIT SOURCE LOCK ★★★
        Edit ONLY IMAGE 1, the current/latest 3D render.
        The original 2D floor plan is NOT provided and must NOT be used.
        Do NOT add old labels, room names, function labels, title blocks, legends, or 2D annotations.

        IMAGE 1 — Current/latest 3D render.

        USER INSTRUCTION: {instruction}

        {strict_edit_scope_lock}

        Apply ONLY the instruction. Everything else must remain identical to IMAGE 1.
        Keep camera angle, dimensions, walls, windows, toilet/bathroom wall colors, floor/wall theme, and unrelated rooms unchanged.
        Output clean professional architectural render with no labels and no unrelated changes.
    """

    REFERENCE_CATEGORY_MAP = {
        "residential_house":     "residential",
        "residential_apartment": "residential",
        "office":                "commercial",
        "retail_shop":           "commercial",
        "restaurant":            "commercial",
        "cafe":                  "commercial",
        "cinema":                "commercial",
        "hotel":                 "commercial",
        "clinic":                "commercial",
        "gym":                   "commercial",
        "warehouse":             "commercial",
        "mixed_use":             "commercial",
    }


# ═══════════════════════════════════════════════════════════════
# RENDERING SERVICE
# ═══════════════════════════════════════════════════════════════

class RenderingService:

    def __init__(self, default_provider: str = "openrouter") -> None:
        self.default_provider = Provider.OPENROUTER.value

        self.client:        Optional[OpenAI] = None
        self.gemini_client                   = None

        try:
            if not ai_settings.OPENROUTER_API_KEY:
                raise ValueError("OPENROUTER_API_KEY is missing")
            self.client = OpenAI(
                base_url=ai_settings.OPENROUTER_BASE_URL,
                api_key=ai_settings.OPENROUTER_API_KEY,
            )
            print("✅ OpenRouter client initialised")
        except Exception as e:
            print(f"⚠️  OpenRouter init error: {e}")

        try:
            if not ai_settings.GOOGLE_CLOUD_PROJECT or not ai_settings.GOOGLE_CLOUD_LOCATION:
                raise ValueError("GOOGLE_CLOUD_PROJECT or GOOGLE_CLOUD_LOCATION missing")
            self.gemini_client = genai.Client(
                vertexai=True,
                project=ai_settings.GOOGLE_CLOUD_PROJECT,
                location=ai_settings.GOOGLE_CLOUD_LOCATION,
            )
            print("✅ Vertex Gemini client initialised")
        except Exception as e:
            print(f"⚠️  Vertex Gemini init error: {e}")

        # Generated images are never written to disk here — they are returned
        # as bytes and stored by the Django adapter under media/. The only
        # thing that still needs a directory is the multi-turn edit history,
        # a few KB of JSON, which lives in the scratch directory.
        self.session_dir = os.path.join(ai_settings.SCRATCH_DIR, "sessions")
        os.makedirs(self.session_dir, exist_ok=True)

        # References are intentionally disabled. This pipeline uses one universal style only.
        self.references_dir   = None
        self._reference_cache = []

        self.session_memory = EditSessionMemory(persist_dir=self.session_dir)

        self._stats = {
            "openrouter_calls":        0,
            "vertex_calls":            0,
            "vertex_fallbacks":        0,
            "stage1_calls":            0,
            "stage2_calls":            0,
            "cache_hits":              0,
            "total_renders":           0,
            "cache_bypasses":          0,
            "style_extraction_calls":  0,  # compatibility only; no active style extraction
        }

        print(f"\n🎯 Provider: {self.default_provider.upper()}")

    # ───────────────────────────────────────────────────────────
    # AGENT ORCHESTRATOR
    # ───────────────────────────────────────────────────────────

    def _decide_agent_action(
        self,
        *,
        current_image_url:    Optional[str],
        edited_image_url:     Optional[str],
        annotated_image_path: Optional[str],
        instruction:          Optional[str],
        force_regenerate:     bool = False,
    ) -> AgentAction:
        if force_regenerate or not current_image_url:
            return AgentAction.GENERATE_FULL
        if annotated_image_path:
            return AgentAction.AREA_EDIT
        if edited_image_url and edited_image_url != current_image_url:
            return AgentAction.STYLE_UPDATE
        if instruction and instruction.strip():
            return AgentAction.CONVERSATIONAL_EDIT
        return AgentAction.GENERATE_FULL

    # ───────────────────────────────────────────────────────────
    # CONFIGURATION HELPERS
    # ───────────────────────────────────────────────────────────

    def _get_openrouter_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if ai_settings.OPENROUTER_SITE_URL:
            headers["HTTP-Referer"] = ai_settings.OPENROUTER_SITE_URL
        if ai_settings.OPENROUTER_APP_NAME:
            headers["X-Title"] = ai_settings.OPENROUTER_APP_NAME
        return headers

    def _get_vertex_image_config(self) -> google_types.GenerateContentConfig:
        return google_types.GenerateContentConfig(
            temperature=0.05, top_k=1, top_p=0.0, seed=42,
            response_modalities=["IMAGE", "TEXT"],
        )

    def _get_vertex_text_config(self) -> google_types.GenerateContentConfig:
        return google_types.GenerateContentConfig(
            temperature=0.0, top_k=1, top_p=0.0, seed=42,
        )

    def _get_provider_model(self, provider: str, text_only: bool = False) -> str:
        if provider == Provider.GEMINI_VERTEX.value:
            return ai_settings.GEMINI_MODEL_ANALYSIS if text_only else ai_settings.GEMINI_MODEL_IMAGE
        return ai_settings.OPENROUTER_MODEL_ANALYSIS if text_only else ai_settings.OPENROUTER_MODEL_RENDER

    def _build_source_info(
        self,
        provider: str,
        text_only: bool = False,
        model_name: Optional[str] = None,
    ) -> Dict[str, str]:
        # `model_name` is passed when a call site overrides the provider default
        # (e.g. the isometric render uses its own OpenRouter model), so logs and
        # asset metadata report the model that actually produced the image.
        model_name     = model_name or self._get_provider_model(provider, text_only=text_only)
        provider_label = "Vertex" if provider == Provider.GEMINI_VERTEX.value else "OpenRouter"
        mode_label     = "analysis" if text_only else "render"
        return {
            "provider":       provider,
            "provider_label": provider_label,
            "model":          model_name,
            "mode":           mode_label,
            "label":          f"{provider_label} | {model_name}",
        }

    def _provider_tag(self, provider: Optional[str]) -> str:
        if provider == Provider.GEMINI_VERTEX.value:
            return "VERTEX"
        if provider == Provider.OPENROUTER.value:
            return "OPENROUTER"
        return "UNKNOWN"

    def _pipeline_provider_route_label(self) -> str:
        configured: List[str] = []
        if self.client:
            configured.append("Primary: OpenRouter")
        if self.gemini_client:
            configured.append("Fallback: Vertex" if self.client else "Vertex")
        return " | ".join(configured) or "No Provider Configured"

    def _log_generation_source(self, stage_label: str, source_info: Dict[str, str]) -> None:
        print(
            f"🛰️  {stage_label} SOURCE: "
            f"{source_info.get('provider_label', source_info.get('provider'))} | "
            f"{source_info.get('model')}"
        )

    def _summarize_response_for_debug(self, response) -> str:
        response_data = self._response_to_dict(response)
        if not response_data:
            text = self._extract_response_text(response)
            return f"text_preview={text[:160]!r}" if text else "no_payload"
        summary_parts: List[str] = []
        top_level_keys = sorted(response_data.keys())[:10]
        if top_level_keys:
            summary_parts.append(f"keys={top_level_keys}")
        extracted_text = self._extract_response_text(response)
        if extracted_text:
            summary_parts.append(f"extracted_text={extracted_text[:160]!r}")
        return "; ".join(summary_parts) or "structured_payload_present"

    # ───────────────────────────────────────────────────────────
    # UTILITIES
    # ───────────────────────────────────────────────────────────

    def _discover_references(self) -> List[str]:
        # Disabled by design: no reference images are used in this pipeline.
        return []

    def _discover_references_by_category(self, category: str) -> List[str]:
        # Disabled by design: all categories use the same universal theme.
        return []

    def _url_to_fs_path(self, url_or_path: str) -> str:
        """Kept as the single normalisation point for incoming paths.

        The `/outputs/<file>` URL form it used to translate belonged to the
        pipeline output directory, which no longer exists: callers hand over
        real filesystem paths to media-stored assets.
        """
        return url_or_path

    def _load_image(self, path: str) -> Optional[PILImage.Image]:
        if not path:
            return None
        candidates = [
            self._url_to_fs_path(path),
            path,
            path.lstrip("/"),
            os.path.abspath(path),
        ]
        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                try:
                    img = PILImage.open(candidate).convert("RGB")
                    return CanvasProcessor.prepare_input(img)
                except Exception:
                    continue
        return None

    def _resolve_path(self, url_or_path: str) -> Optional[str]:
        candidates = [
            self._url_to_fs_path(url_or_path),
            url_or_path,
            url_or_path.lstrip("/"),
            os.path.abspath(url_or_path),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return os.path.abspath(c)
        return None

    def _resolve_url_to_path(self, url_or_path: str) -> str:
        return self._url_to_fs_path(url_or_path)


    def _floor_plan_session_key(self, floor_plan_url: Optional[str]) -> str:
        """
        Stable key for edit history. This may be the original floor-plan path,
        but the original floor-plan image is NEVER loaded/passed in edit mode.
        """
        if not floor_plan_url:
            return "__default_edit_session__"
        resolved = self._resolve_path(floor_plan_url)
        return resolved or floor_plan_url

    def _select_edit_source_path(
        self,
        *,
        selected_3d_path: Optional[str] = None,
        latest_3d_path: Optional[str] = None,
        floor_plan_url: Optional[str] = None,
    ) -> str:
        """
        Edit mode source selection.

        Priority:
          1. user-selected/edited 3D render
          2. current/latest 3D render
          3. last render from session memory

        Absolute rule:
          Original 2D floor plan must NEVER be returned as an edit source.
        """
        candidates: List[Optional[str]] = [selected_3d_path, latest_3d_path]

        session_key = self._floor_plan_session_key(floor_plan_url)
        last = self.session_memory.last_render(session_key)
        if last:
            candidates.append(last.render_abs_path)

        # Backward compatibility: older records may have used raw floor_plan_url as the key.
        if floor_plan_url and session_key != floor_plan_url:
            last_raw = self.session_memory.last_render(floor_plan_url)
            if last_raw:
                candidates.append(last_raw.render_abs_path)

        floor_plan_resolved = self._resolve_path(floor_plan_url) if floor_plan_url else None

        for candidate in candidates:
            resolved = self._resolve_path(candidate) if candidate else None
            if not resolved:
                continue
            if floor_plan_resolved and os.path.abspath(resolved) == os.path.abspath(floor_plan_resolved):
                continue
            return resolved

        raise ValueError(
            "No editable 3D render found. Edit mode requires selected/current/latest 3D render. "
            "Original 2D floor plan is not allowed as edit source."
        )

    def _strict_edit_scope_lock(self, instruction: Optional[str]) -> str:
        safe_instruction = (instruction or "").strip() or "No extra change requested."
        return Prompts._STRICT_EDIT_SCOPE_LOCK.format(instruction=safe_instruction)


    def _map_error(self, error_str: str) -> str:
        if not error_str:
            return "An unexpected error occurred. Please try again."
        err_up = error_str.upper()
        if "429" in err_up or "RESOURCE_EXHAUSTED" in err_up or "QUOTA" in err_up:
            return "AI service is busy. Please wait and try again."
        if "404" in err_up or "NOT_FOUND" in err_up or "MODEL_NOT_FOUND" in err_up:
            return "AI engine is under maintenance. Please try again shortly."
        if "500" in err_up or "503" in err_up or "INTERNAL_ERROR" in err_up:
            return "AI backend is temporarily unresponsive. Please try again shortly."
        if "SAFETY" in err_up or "HARM" in err_up:
            return "Request triggered AI safety filters and couldn't be processed."
        return error_str

    def _should_fallback_to_vertex(self, error_str: str) -> bool:
        err_up = (error_str or "").upper()
        markers = (
            "429", "500", "502", "503", "504",
            "RESOURCE_EXHAUSTED", "QUOTA", "OVERLOAD",
            "SERVER DOWN", "TIMEOUT", "TIMED OUT",
            "ECONNRESET", "SERVICE UNAVAILABLE", "INTERNAL_ERROR",
            "RATE LIMIT", "TOO MANY REQUESTS",
            "CONNECTION ERROR", "CONNECTION REFUSED", "CONNECTION RESET",
            "CONNECTIONERROR", "NETWORKERROR", "NETWORK ERROR",
            "FAILED TO CONNECT", "CANNOT CONNECT", "UNREACHABLE",
            "NAME OR SERVICE NOT KNOWN", "NODENAME NOR SERVNAME",
            "BROKEN PIPE", "REMOTEDISCONNECTED", "REMOTEPROTOCOLERROR",
            "NO IMAGE DATA IN OPENROUTER RESPONSE",
            "FAILED TO READ GENERATED IMAGE FROM OPENROUTER RESPONSE",
        )
        return any(m in err_up for m in markers)

    def _build_area_edit_region_guide(
        self,
        current_img:   PILImage.Image,
        annotated_img: PILImage.Image,
        region_mask:   Optional[PILImage.Image] = None,
    ) -> PILImage.Image:
        filtered_mask = region_mask or self._extract_area_edit_mask(current_img, annotated_img)

        # A heavy wash used to be painted over the region at ~65% opacity,
        # which hid the very content the model has to recognise before it can
        # apply the instruction to it ("modernise THIS furniture"). Mark the
        # zone instead: a light tint for the area plus a hard contour for the
        # boundary, so the region is unambiguous and still fully readable.
        guide = current_img.convert("RGBA")
        solid = filtered_mask.point(lambda px: 255 if px >= 128 else 0, mode="L")

        tint = PILImage.new("RGBA", current_img.size, (0, 200, 255, 0))
        tint.putalpha(filtered_mask.point(lambda px: (px * 60) // 255))
        guide = PILImage.alpha_composite(guide, tint)

        contour_width = max(3, (min(current_img.size) // 150) | 1)
        contour = ImageChops.subtract(solid, solid.filter(ImageFilter.MinFilter(contour_width)))
        line = PILImage.new("RGBA", current_img.size, (0, 229, 255, 0))
        line.putalpha(contour)

        return PILImage.alpha_composite(guide, line).convert("RGB")

    def _extract_area_edit_mask(
        self,
        current_img:   PILImage.Image,
        annotated_img: PILImage.Image,
    ) -> PILImage.Image:
        """Return a filled, base-resolution L mask for an area edit.

        New clients upload an exact black/white mask. The legacy annotation
        fallback remains for already-open browser sessions, but deliberately
        avoids treating ordinary resize noise as the selected region.
        """
        current_img = current_img.convert("RGB")
        annotated_rgb = annotated_img.convert("RGB")

        # Exact masks contain only near-black/near-white neutral colours. Check
        # before resizing so browser nearest-pixel mask data stays unambiguous.
        red, green, blue = annotated_rgb.split()
        is_neutral = (
            ImageChops.difference(red, green).getextrema()[1] <= 2
            and ImageChops.difference(red, blue).getextrema()[1] <= 2
        )
        gray_histogram = annotated_rgb.convert("L").histogram()
        pixel_count = annotated_rgb.width * annotated_rgb.height
        dark_pixels = sum(gray_histogram[:16])
        bright_pixels = sum(gray_histogram[240:])
        # Canvas polygon edges are anti-aliased and therefore include mid-grey
        # pixels. Neutral channels plus a dominant black background is the
        # reliable protocol marker; do not require only two exact colours.
        is_explicit_mask = is_neutral and (
            (dark_pixels >= pixel_count * 0.20 and bright_pixels > 0)
            or bright_pixels >= pixel_count * 0.95
        )
        if is_explicit_mask:
            if annotated_rgb.size != current_img.size:
                annotated_rgb = annotated_rgb.resize(
                    current_img.size, resample=PILImage.Resampling.NEAREST
                )
            mask = annotated_rgb.convert("L").point(
                lambda px: 255 if px >= 128 else 0, mode="L"
            )
            mask = self._fill_mask_holes(mask)
            if not mask.getbbox():
                raise ValueError("The area-edit mask is empty. Trace a region before regenerating.")
            # A blank/near-white sheet satisfies the neutral+bright test above
            # and would otherwise resolve to the entire frame, handing the
            # model a licence to re-render the whole render. Refuse instead:
            # that input is a protocol mismatch, not a selection.
            self._reject_full_frame_region(mask)
            return self._feather_mask(mask)

        return self._region_from_trace(current_img, annotated_img)

    # ── Rough traced-region resolution ─────────────────────────────
    # The user does not trace precisely: they scribble a rough circle or box
    # around the thing they mean, in whatever colour the canvas is set to,
    # and the stroke is frequently left slightly open. Every threshold below
    # exists to turn that gesture into the region they meant.

    # Working resolution for trace analysis. A rough gesture carries no
    # pixel-level intent, and downscaling both suppresses anti-aliasing
    # noise and keeps the pure-Python component scan fast.
    _TRACE_PROBE_DIM = 512
    # Colour distance from the base render that counts as drawn-on. Well
    # below the old 72: a green stroke over a green sofa, or a light stroke
    # over a light floor, is a low-contrast but perfectly deliberate trace.
    _TRACE_DIFF_THRESHOLD = 36
    # Saturation jump that marks a stroke. Marker colours are vivid and these
    # renders are not, so this catches strokes the colour distance misses.
    _TRACE_CHROMA_THRESHOLD = 40
    # How close a pixel must sit to the learned stroke colour to be read as
    # part of the same stroke, once that colour is known.
    _TRACE_COLOUR_TOLERANCE = 44
    # The colour pass is rejected if it selects more than this share of the
    # frame: that means the stroke colour also matches real content (a red
    # trace over a red floor), and the seed alone is the safer answer.
    _TRACE_COLOUR_MAX_SHARE = 0.45
    # A filled component this far below its own convex hull means the stroke
    # never closed and the flood fill leaked out of it.
    _TRACE_FILL_HULL_RATIO = 0.55
    # Above this share of the frame the "trace" is a detection failure, not a
    # selection — better to say so than to silently re-render everything.
    _TRACE_MAX_FRAME_SHARE = 0.97

    def _reject_full_frame_region(self, region: PILImage.Image) -> None:
        """Refuse a "selection" that is really the whole frame.

        Every enforcement this feature has rests on there being an outside to
        preserve; a region covering everything silently turns a local edit
        into a full re-render, which is the worst outcome available here.
        """
        total = region.width * region.height
        if not total:
            return
        covered = sum(1 for px in region.getdata() if px) / total
        if covered > self._TRACE_MAX_FRAME_SHARE:
            raise ValueError(
                "The traced region covers the whole image. Trace a smaller area to edit."
            )

    def _trace_stroke_mask(
        self,
        current_probe:   PILImage.Image,
        annotated_probe: PILImage.Image,
    ) -> PILImage.Image:
        """Binary mask of the drawn stroke, whatever colour it was drawn in."""
        colour_diff = self._max_channel_diff(current_probe, annotated_probe)
        drawn = colour_diff.point(
            lambda px: 255 if px >= self._TRACE_DIFF_THRESHOLD else 0, mode="L"
        )

        # Saturation channel only: a vivid stroke laid over muted architecture
        # registers here even when its luminance nearly matches the surface.
        current_saturation = current_probe.convert("HSV").split()[1]
        annotated_saturation = annotated_probe.convert("HSV").split()[1]
        chroma_gain = ImageChops.subtract(annotated_saturation, current_saturation)
        vivid = chroma_gain.point(
            lambda px: 255 if px >= self._TRACE_CHROMA_THRESHOLD else 0, mode="L"
        )

        seed = ImageChops.lighter(drawn, vivid)
        return ImageChops.lighter(seed, self._stroke_colour_pass(annotated_probe, seed))

    def _stroke_colour_pass(
        self,
        annotated_probe: PILImage.Image,
        seed:            PILImage.Image,
    ) -> PILImage.Image:
        """Recover stroke segments that blend into the content beneath them.

        The trace is one uniform colour, but it is only *visible* where it
        crosses something of a different colour — a red loop over a red-brown
        floor, or a blue one over blue window glass, registers on part of its
        length and vanishes on the rest. That partial ring is what made the
        same gesture land a good edit one time and a lopsided one the next.
        So learn the colour from the segments that did register, then take
        every pixel wearing it. Fails soft: if that colour also matches real
        content, the pass is dropped rather than swallowing the room.
        """
        seed_bbox = seed.getbbox()
        if seed_bbox is None:
            return PILImage.new("L", annotated_probe.size, 0)

        # Modal colour of the seed, on a coarse grid so anti-aliased edges
        # collapse onto the stroke's true colour instead of splitting it.
        buckets: Dict[Tuple[int, int, int], int] = defaultdict(int)
        seed_pixels = seed.load()
        annotated_pixels = annotated_probe.load()
        for y in range(seed_bbox[1], seed_bbox[3]):
            for x in range(seed_bbox[0], seed_bbox[2]):
                if not seed_pixels[x, y]:
                    continue
                r, g, b = annotated_pixels[x, y]
                buckets[(r // 16, g // 16, b // 16)] += 1
        if not buckets:
            return PILImage.new("L", annotated_probe.size, 0)

        bucket = max(buckets, key=buckets.get)
        stroke_colour = (bucket[0] * 16 + 8, bucket[1] * 16 + 8, bucket[2] * 16 + 8)

        distance = self._max_channel_diff(
            annotated_probe, PILImage.new("RGB", annotated_probe.size, stroke_colour)
        )
        matched = distance.point(
            lambda px: 255 if px <= self._TRACE_COLOUR_TOLERANCE else 0, mode="L"
        )

        total = annotated_probe.width * annotated_probe.height
        share = sum(1 for px in matched.getdata() if px) / total if total else 0.0
        if share > self._TRACE_COLOUR_MAX_SHARE:
            return PILImage.new("L", annotated_probe.size, 0)
        return matched

    @staticmethod
    def _convex_hull(points: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """Andrew's monotone chain hull around the traced stroke."""
        ordered = sorted(set(points))
        if len(ordered) < 3:
            return ordered

        def half(sequence):
            chain: List[Tuple[int, int]] = []
            for point in sequence:
                while len(chain) >= 2:
                    (ax, ay), (bx, by) = chain[-2], chain[-1]
                    cross = (bx - ax) * (point[1] - ay) - (by - ay) * (point[0] - ax)
                    if cross > 0:
                        break
                    chain.pop()
                chain.append(point)
            return chain

        lower = half(ordered)
        upper = half(reversed(ordered))
        return lower[:-1] + upper[:-1]

    def _region_from_trace(
        self,
        current_img:   PILImage.Image,
        annotated_img: PILImage.Image,
    ) -> PILImage.Image:
        """Resolve a rough, any-colour, possibly-open trace into a solid region.

        Each drawn stroke is handled on its own so two separate traces stay
        two separate regions. Per stroke: bridge the gaps a hand-drawn line
        leaves, fill what the closed contour encloses, and fall back to the
        stroke's convex hull when the fill leaked — which is what used to
        make this feature land the edit on a thin ring instead of the region,
        and is the difference between "worked perfectly" and "did nothing"
        between two attempts at the same trace.
        """
        current_probe = self._probe_to(current_img, self._TRACE_PROBE_DIM)
        annotated_probe = self._probe_to(annotated_img, self._TRACE_PROBE_DIM)
        if annotated_probe.size != current_probe.size:
            annotated_probe = annotated_probe.resize(
                current_probe.size, resample=PILImage.Resampling.LANCZOS
            )

        stroke = self._trace_stroke_mask(current_probe, annotated_probe)
        width, height = stroke.size
        short_side = min(width, height)

        # Bridge the gaps in a hand-drawn outline so the fill cannot escape
        # through them. This also grows the region slightly outward, which is
        # wanted: a rough trace usually clips the very object it circles, and
        # a hard edge through a sofa is worse than a little slack.
        bridge = max(5, (short_side // 30) | 1)
        stroke = stroke.filter(ImageFilter.MaxFilter(bridge))

        components = self._mask_components(stroke)
        if not components:
            raise ValueError("Could not detect a traced edit region. Please trace the area again.")

        # Drop specks, but never the trace itself: if everything looks like a
        # speck, keep the largest so a small deliberate trace still works.
        min_area = max(12, (width * height) // 4000)
        kept = [c for c in components if len(c) >= min_area]
        if not kept:
            kept = [max(components, key=len)]

        region = PILImage.new("L", stroke.size, 0)
        for coords in self._group_trace_arcs(kept):
            patch = PILImage.new("L", stroke.size, 0)
            patch_pixels = patch.load()
            for x, y in coords:
                patch_pixels[x, y] = 255

            filled = self._fill_mask_holes(patch)
            filled_area = sum(1 for px in filled.getdata() if px)

            hull_patch = None
            hull_area = 0
            hull = self._convex_hull(coords)
            if len(hull) >= 3:
                hull_patch = PILImage.new("L", stroke.size, 0)
                ImageDraw.Draw(hull_patch).polygon(hull, fill=255)
                hull_area = sum(1 for px in hull_patch.getdata() if px)

            leaked = hull_area and filled_area < hull_area * self._TRACE_FILL_HULL_RATIO
            # A leaked fill means the stroke never closed, so fall back to
            # what the user drew around rather than to the stroke itself.
            region = ImageChops.lighter(region, hull_patch if leaked else filled)

        if not region.getbbox():
            raise ValueError("Could not detect a traced edit region. Please trace the area again.")

        self._reject_full_frame_region(region)

        region = region.resize(current_img.size, resample=PILImage.Resampling.BILINEAR)
        return self._feather_mask(region, radius=max(4, min(current_img.size) // 200))

    # Two pieces of stroke are read as arcs of one gesture when the gap
    # between them is within this multiple of the smaller piece's own size.
    # Biased towards merging on purpose: over-merging widens the edit zone a
    # little, while under-merging hulls each arc into a sliver and the edit
    # visibly fails to land — the failure the user actually sees.
    _TRACE_ARC_GAP_FACTOR = 1.5

    def _group_trace_arcs(
        self,
        components: List[List[Tuple[int, int]]],
    ) -> List[List[Tuple[int, int]]]:
        """Merge stroke pieces that are arcs of a single traced loop.

        Where a trace runs over content close to its own colour it registers
        in pieces — sometimes only as blobs at the loop's corners — and each
        piece on its own hulls into a sliver, so the union is nothing like
        the region the user drew around. Arcs of one loop land close together
        relative to their own size, so that is the test; two deliberately
        separate traces sit far apart and stay separate regions.
        """
        def bounds(coords):
            xs = [x for x, _ in coords]
            ys = [y for _, y in coords]
            return min(xs), min(ys), max(xs), max(ys)

        groups = [{"coords": list(coords), "bbox": bounds(coords)} for coords in components]

        merging = True
        while merging:
            merging = False
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    ax1, ay1, ax2, ay2 = groups[i]["bbox"]
                    bx1, by1, bx2, by2 = groups[j]["bbox"]
                    # Negative gap means the boxes already overlap.
                    gap_x = max(0, max(ax1, bx1) - min(ax2, bx2))
                    gap_y = max(0, max(ay1, by1) - min(ay2, by2))
                    reach = self._TRACE_ARC_GAP_FACTOR * min(
                        max(ax2 - ax1, ay2 - ay1),
                        max(bx2 - bx1, by2 - by1),
                    )
                    if gap_x > reach or gap_y > reach:
                        continue
                    groups[i]["coords"].extend(groups[j]["coords"])
                    groups[i]["bbox"] = (
                        min(ax1, bx1), min(ay1, by1), max(ax2, bx2), max(ay2, by2),
                    )
                    groups.pop(j)
                    merging = True
                    break
                if merging:
                    break

        return [group["coords"] for group in groups]

    @staticmethod
    def _mask_components(mask: PILImage.Image) -> List[List[Tuple[int, int]]]:
        """Connected runs of set pixels, each as its own coordinate list."""
        width, height = mask.size
        pixels = mask.load()
        visited = bytearray(width * height)
        components: List[List[Tuple[int, int]]] = []

        for y in range(height):
            for x in range(width):
                if not pixels[x, y] or visited[y * width + x]:
                    continue
                visited[y * width + x] = 1
                stack = [(x, y)]
                coords: List[Tuple[int, int]] = []
                while stack:
                    cx, cy = stack.pop()
                    coords.append((cx, cy))
                    for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                        if 0 <= nx < width and 0 <= ny < height:
                            index = ny * width + nx
                            if pixels[nx, ny] and not visited[index]:
                                visited[index] = 1
                                stack.append((nx, ny))
                components.append(coords)

        return components

    @staticmethod
    def _probe_to(img: PILImage.Image, max_dim: int) -> PILImage.Image:
        """RGB copy scaled so its longest side is at most `max_dim`."""
        rgb = img.convert("RGB")
        if max(rgb.size) <= max_dim:
            return rgb
        scale = max_dim / max(rgb.size)
        return rgb.resize(
            (max(int(rgb.width * scale), 1), max(int(rgb.height * scale), 1)),
            resample=PILImage.Resampling.LANCZOS,
        )

    @staticmethod
    def _fill_mask_holes(mask: PILImage.Image) -> PILImage.Image:
        """Fill any area enclosed by a closed traced outline.

        A hand-traced boundary (the user draws around a room, not a filled
        blob) diffs to a thin ring of changed pixels — the room's interior
        never differs from the base render, so it is never in the raw mask.
        Flood-filling from the image border marks every zero pixel that IS
        reachable as "outside"; whatever zero pixel is left unreached is
        enclosed by the trace and belongs in the edit region.
        """
        width, height = mask.size
        pixels = mask.load()
        outside = bytearray(width * height)
        stack = []

        def seed(x: int, y: int) -> None:
            idx = y * width + x
            if pixels[x, y] == 0 and not outside[idx]:
                outside[idx] = 1
                stack.append((x, y))

        for x in range(width):
            seed(x, 0)
            seed(x, height - 1)
        for y in range(height):
            seed(0, y)
            seed(width - 1, y)

        while stack:
            cx, cy = stack.pop()
            for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                if 0 <= nx < width and 0 <= ny < height:
                    idx = ny * width + nx
                    if pixels[nx, ny] == 0 and not outside[idx]:
                        outside[idx] = 1
                        stack.append((nx, ny))

        filled = mask.copy()
        filled_pixels = filled.load()
        for y in range(height):
            for x in range(width):
                if pixels[x, y] == 0 and not outside[y * width + x]:
                    filled_pixels[x, y] = 255
        return filled

    @staticmethod
    def _feather_mask(mask: PILImage.Image, radius: int = 4) -> PILImage.Image:
        """Soften the mask edge so the post-generation composite blends the
        edit into the untouched original instead of leaving a hard seam."""
        return mask.filter(ImageFilter.GaussianBlur(radius=radius))

    # How far the framing signature may move before an in-place edit is
    # treated as having swung the camera instead of editing the scene.
    _CAMERA_DRIFT_TOLERANCE = 0.22

    @staticmethod
    def _frame_signature(img: PILImage.Image) -> Optional[float]:
        """How much of the content's bounding box its own corners occupy.

        A plan-aligned building fills its bounding box, so all four corners
        hold content; a view swung into a rotated 3/4 diamond leaves those
        corners empty while the diamond's points touch the edge midpoints.
        Comparing this number between an edit's source and its result flags a
        camera that moved, and ignores content changes (new furniture does not
        move the footprint's corners), which is exactly the distinction an
        in-place edit has to hold. Returns None when it cannot be measured.
        """
        try:
            probe = img.convert("RGB")
            if max(probe.size) > 512:
                scale = 512 / max(probe.size)
                probe = probe.resize(
                    (max(int(probe.width * scale), 1), max(int(probe.height * scale), 1)),
                    resample=PILImage.Resampling.LANCZOS,
                )

            w, h = probe.size
            corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
            samples = [probe.getpixel(p) for p in corners]
            background = tuple(sum(s[i] for s in samples) // len(samples) for i in range(3))

            bg_img = PILImage.new("RGB", probe.size, background)
            content = ImageChops.difference(probe, bg_img).convert("L").point(
                lambda px: 255 if px > 26 else 0, mode="L"
            )
            bbox = content.getbbox()
            if not bbox:
                return None

            x1, y1, x2, y2 = bbox
            box_w, box_h = x2 - x1, y2 - y1
            if box_w < 24 or box_h < 24:
                return None

            patch = max(6, min(box_w, box_h) // 8)
            regions = [
                (x1, y1, x1 + patch, y1 + patch),
                (x2 - patch, y1, x2, y1 + patch),
                (x1, y2 - patch, x1 + patch, y2),
                (x2 - patch, y2 - patch, x2, y2),
            ]

            fills = []
            for region in regions:
                crop = content.crop(region)
                total = crop.width * crop.height
                if not total:
                    continue
                filled = sum(1 for px in crop.getdata() if px)
                fills.append(filled / total)
            if not fills:
                return None
            return sum(fills) / len(fills)
        except Exception:
            return None

    def _framing_delta(
        self,
        response,
        source_signature: Optional[float],
    ) -> Optional[float]:
        """How far a generated response's framing sits from the source's."""
        if source_signature is None:
            return None
        try:
            generated = PILImage.open(io.BytesIO(self._extract_image_bytes(response)))
        except Exception:
            return None
        generated_signature = self._frame_signature(generated)
        if generated_signature is None:
            return None
        return abs(generated_signature - source_signature)

    def _image_from_response(self, response) -> Optional[PILImage.Image]:
        """Decode a generation response to RGB, or None if it cannot be read."""
        try:
            return PILImage.open(io.BytesIO(self._extract_image_bytes(response))).convert("RGB")
        except Exception:
            return None

    # ── In-place edit fidelity ─────────────────────────────────────
    # These models answer by regenerating the whole frame, so an edit that
    # was meant to touch one thing arrives with the rest of the render
    # re-interpreted: texture smoothed into a flatter/cartoonish look,
    # exposure and background shifted, and the dimension/annotation layer
    # re-authored with invented numbers. Nothing in the prompt can guarantee
    # otherwise, so the untouched pixels are restored deterministically and
    # what remains is measured and given one corrective retry.

    # Per-pixel change that reads as a deliberate edit rather than drift.
    _EDIT_KEEP_THRESHOLD = 34
    # Guard rails: below this the model effectively changed nothing worth
    # keeping, above it it re-rendered the scene and there is no clean
    # "untouched" majority left to restore.
    _EDIT_KEEP_MIN_FRACTION = 0.003
    _EDIT_KEEP_MAX_FRACTION = 0.85
    # Fallback outer frame ring, used only when the building mass cannot be
    # isolated. The real annotation zone is derived from the geometry
    # instead (see _building_mass_mask): dimension lines sit well inside the
    # frame on these renders, so a fixed ring is not a safe stand-in.
    _ANNOTATION_BAND = 0.09
    _ANNOTATION_DRIFT_TOLERANCE = 0.04
    # Working resolution for the coarse region masks.
    _MASS_PROBE_DIM = 256
    # How much of the source's fine detail must survive the edit.
    _DETAIL_RETENTION_FLOOR = 0.72

    @staticmethod
    def _max_channel_diff(a: PILImage.Image, b: PILImage.Image) -> PILImage.Image:
        """Per-pixel L-mode magnitude of change across all three channels."""
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
        dr, dg, db = diff.split()
        return ImageChops.lighter(ImageChops.lighter(dr, dg), db)

    def _probe(self, img: PILImage.Image) -> PILImage.Image:
        """Downscaled copy for the coarse region work."""
        return self._probe_to(img, self._MASS_PROBE_DIM)

    def _building_mass_mask(
        self,
        img:  PILImage.Image,
        size: Optional[Tuple[int, int]] = None,
    ) -> Optional[PILImage.Image]:
        """Solid mask of the building itself, with annotations excluded.

        The building is one large filled mass; dimension lines, arrowheads
        and measurement text are thin strokes sitting in the surrounding
        margin. A morphological opening erases the strokes and keeps the
        mass, so its complement is the annotation zone — wherever it happens
        to fall in the frame, which a fixed border ring cannot capture (on
        these renders the dimension lines sit well inside the frame edge).
        """
        try:
            probe = self._probe(img)
            corners = [
                (0, 0), (probe.width - 1, 0),
                (0, probe.height - 1), (probe.width - 1, probe.height - 1),
            ]
            samples = [probe.getpixel(p) for p in corners]
            background = tuple(sum(s[i] for s in samples) // len(samples) for i in range(3))

            content = self._max_channel_diff(
                probe, PILImage.new("RGB", probe.size, background)
            ).point(lambda px: 255 if px > 26 else 0, mode="L")

            mass = content.filter(ImageFilter.MinFilter(9)).filter(ImageFilter.MaxFilter(9))
            if not mass.getbbox():
                return None
            return mass.resize(size or img.size, resample=PILImage.Resampling.NEAREST)
        except Exception:
            return None

    # An instruction naming the annotation layer itself is allowed to change
    # it; anything else must leave the measurement ring alone.
    _ANNOTATION_KEYWORDS = (
        "dimension", "measurement", "measure", "label", "annotation", "callout",
        "scale bar", "ruler", "legend", "title block", "north arrow",
    )

    @classmethod
    def _targets_annotations(cls, instruction: Optional[str]) -> bool:
        text = (instruction or "").lower()
        return any(keyword in text for keyword in cls._ANNOTATION_KEYWORDS)

    def _deliberate_edit_mask(
        self,
        source_img:    PILImage.Image,
        generated_img: PILImage.Image,
        protect_annotation_band: bool = True,
    ) -> Optional[PILImage.Image]:
        """Mask of where the edit actually landed.

        Everything outside it is restored from the source, which is what
        keeps the original clarity, texture, materials, sharpness, exposure
        and annotations bit-exact. Reverting these pixels cannot introduce a
        visible seam: they are, by construction, the pixels the model left
        within noise distance of the source. Returns None when the result
        should be kept whole (nothing meaningful changed, or the model
        re-rendered so much that there is no untouched majority to restore).
        """
        try:
            if generated_img.size != source_img.size:
                generated_img = generated_img.resize(
                    source_img.size, resample=PILImage.Resampling.LANCZOS
                )

            diff = self._max_channel_diff(source_img, generated_img)
            binary = diff.point(
                lambda px: 255 if px >= self._EDIT_KEEP_THRESHOLD else 0, mode="L"
            )
            # Opening drops the speckle of a re-render's noise floor; the
            # dilation then re-grows each real edit to carry its own soft
            # edge, contact shadow and reflection with it.
            binary = binary.filter(ImageFilter.MinFilter(5))
            binary = binary.filter(ImageFilter.MaxFilter(11))

            # Blurring a thin, high-contrast feature produces a large pixel
            # diff, so smoothed dimension lines and measurement text would
            # otherwise read as deliberate edits and survive. Nothing outside
            # the building is the target of a scene edit, so confine edits to
            # the building mass and let the annotation zone restore from the
            # source.
            if protect_annotation_band:
                allowed = self._building_mass_mask(source_img, size=binary.size)
                if allowed is None:
                    w, h = binary.size
                    band = max(4, int(min(w, h) * self._ANNOTATION_BAND))
                    allowed = PILImage.new("L", binary.size, 0)
                    allowed.paste(255, (band, band, max(w - band, band), max(h - band, band)))
                binary = ImageChops.darker(binary, allowed)

            if not binary.getbbox():
                return None

            total = binary.width * binary.height
            kept = sum(1 for px in binary.getdata() if px)
            fraction = kept / total if total else 0.0
            if fraction < self._EDIT_KEEP_MIN_FRACTION or fraction > self._EDIT_KEEP_MAX_FRACTION:
                return None

            return self._feather_mask(binary, radius=3)
        except Exception as e:
            print(f"⚠️  Edit-mask build failed (keeping full output): {e}")
            return None

    @staticmethod
    def _detail_energy(img: PILImage.Image) -> Optional[float]:
        """Mean edge energy — how much fine texture/linework an image holds."""
        try:
            probe = img.convert("L")
            if max(probe.size) > 768:
                scale = 768 / max(probe.size)
                probe = probe.resize(
                    (max(int(probe.width * scale), 1), max(int(probe.height * scale), 1)),
                    resample=PILImage.Resampling.LANCZOS,
                )
            edges = probe.filter(ImageFilter.FIND_EDGES)
            w, h = edges.size
            if w <= 6 or h <= 6:
                return None
            # FIND_EDGES leaves a bright frame on the outermost pixels.
            edges = edges.crop((2, 2, w - 2, h - 2))
            data = edges.getdata()
            return sum(data) / len(data) if len(data) else None
        except Exception:
            return None

    def _annotation_zone_change(
        self,
        source_img:    PILImage.Image,
        generated_img: PILImage.Image,
    ) -> Optional[float]:
        """Mean normalised change outside the building (0.0 - 1.0).

        That zone holds the dimension lines, measurement text and background,
        none of which a scene edit should touch — so any real movement here
        means the annotation layer was re-authored or the exposure shifted.
        """
        try:
            source_probe = self._probe(source_img)
            generated_probe = self._probe(generated_img)
            if generated_probe.size != source_probe.size:
                generated_probe = generated_probe.resize(
                    source_probe.size, resample=PILImage.Resampling.LANCZOS
                )

            zone = self._building_mass_mask(source_img, size=source_probe.size)
            if zone is None:
                w, h = source_probe.size
                band = max(2, int(min(w, h) * self._ANNOTATION_BAND))
                zone = PILImage.new("L", source_probe.size, 0)
                zone.paste(255, (band, band, max(w - band, band), max(h - band, band)))
            zone = ImageChops.invert(zone)

            diff = self._max_channel_diff(source_probe, generated_probe)
            counted = sum(1 for px in zone.getdata() if px)
            if counted < 64:
                return None
            total = sum(ImageChops.darker(diff, zone).getdata())
            return (total / counted) / 255.0
        except Exception:
            return None

    def _edit_drift_hints(
        self,
        source_img:       PILImage.Image,
        generated_img:    Optional[PILImage.Image],
        source_signature: Optional[float],
    ) -> List[str]:
        """Correction hints for whatever an in-place edit failed to preserve."""
        if generated_img is None:
            return []

        hints: List[str] = []

        generated_signature = self._frame_signature(generated_img)
        if (
            source_signature is not None
            and generated_signature is not None
            and abs(generated_signature - source_signature) > self._CAMERA_DRIFT_TOLERANCE
        ):
            hints.append(
                "CAMERA: you re-projected the scene instead of editing it in place. Keep IMAGE 1's "
                "exact camera, projection, rotation, tilt, zoom, crop, centring and aspect ratio."
            )

        zone_change = self._annotation_zone_change(source_img, generated_img)
        if zone_change is not None and zone_change > self._ANNOTATION_DRIFT_TOLERANCE:
            hints.append(
                "ANNOTATIONS: you rewrote the dimension/measurement layer around the building. Copy "
                "IMAGE 1's existing dimension lines, arrowheads and numbers across pixel-for-pixel — "
                "same values, same wording, same positions, same style. Do not re-letter them, do not "
                "recompute or invent measurements, do not add room-name labels, and do not add a scale "
                "bar or ruler. Keep IMAGE 1's background tone and exposure too."
            )

        source_detail = self._detail_energy(source_img)
        generated_detail = self._detail_energy(generated_img)
        if (
            source_detail
            and generated_detail is not None
            and generated_detail < source_detail * self._DETAIL_RETENTION_FLOOR
        ):
            hints.append(
                "DETAIL: your output is smoother and flatter than IMAGE 1 — you lost texture and "
                "sharpness. Match IMAGE 1's exact render quality: same material detail (floor plank "
                "grain, tile, fabric weave), same crisp outlines and edge definition, same contrast "
                "and micro-shading. Do not stylise, soften, simplify or smooth the render, and do not "
                "make it look cartoonish or plastic."
            )

        return hints

    # ───────────────────────────────────────────────────────────
    # GENERATION HELPERS
    # ───────────────────────────────────────────────────────────

    def _pil_to_data_url(self, image: PILImage.Image) -> str:
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{img_str}"

    def _build_openrouter_content(self, contents: list) -> List[Dict]:
        built: List[Dict] = []
        for item in contents:
            if item is None:
                continue
            if isinstance(item, str):
                built.append({"type": "text", "text": item})
            elif isinstance(item, PILImage.Image):
                built.append({
                    "type":      "image_url",
                    "image_url": {"url": self._pil_to_data_url(item)},
                })
            elif isinstance(item, dict):
                built.append(item)
            else:
                raise TypeError(f"Unsupported content type for OpenRouter: {type(item)!r}")
        return built

    def _generate_openrouter(
        self,
        contents:   list,
        model_name: Optional[str] = None,
        max_tokens: Optional[int] = None,
        image_output: bool = True,
    ) -> Tuple[object, List]:
        if not self.client:
            raise Exception("OpenRouter client not initialised")
        self._stats["openrouter_calls"] += 1
        payload: Dict[str, Any] = {
            "model":    model_name or ai_settings.OPENROUTER_MODEL_RENDER,
            "messages": [{"role": "user", "content": self._build_openrouter_content(contents)}],
            "temperature": 0.0, "top_p": 0.0, "seed": 42,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if image_output:
            payload["modalities"] = ["image", "text"]
        headers = self._get_openrouter_headers()
        if headers:
            payload["extra_headers"] = headers
        try:
            response = self.client.chat.completions.create(**payload)
        except Exception as exc:
            # The determinism knobs above are tuned for Gemini image models.
            # Some other OpenRouter image models (e.g. OpenAI's) reject them
            # with a 400 instead of ignoring them, which would otherwise look
            # like a hard provider failure. Drop them and retry once.
            if not self._is_unsupported_sampling_param_error(str(exc)):
                raise
            print(f"⚠️  {payload['model']} rejected sampling params; retrying without them: {exc}")
            for key in ("temperature", "top_p", "seed"):
                payload.pop(key, None)
            response = self.client.chat.completions.create(**payload)
        return response, []

    @staticmethod
    def _is_unsupported_sampling_param_error(error_str: str) -> bool:
        err_up = (error_str or "").upper()
        rejection = any(
            marker in err_up
            for marker in (
                "UNSUPPORTED PARAMETER", "UNSUPPORTED_PARAMETER",
                "UNSUPPORTED VALUE", "UNKNOWN PARAMETER",
                "UNRECOGNIZED REQUEST ARGUMENT", "NOT SUPPORTED",
                "DOES NOT SUPPORT", "INVALID PARAMETER",
            )
        )
        names = any(name in err_up for name in ("TEMPERATURE", "TOP_P", "SEED"))
        return rejection and names

    def _generate_openrouter_text(self, contents: list) -> Tuple[object, List]:
        return self._generate_openrouter(
            contents=contents,
            model_name=ai_settings.OPENROUTER_MODEL_ANALYSIS,
            max_tokens=3000,
            image_output=False,
        )

    def _generate_vertex(
        self,
        contents:   list,
        model_name: Optional[str] = None,
        config:     Optional[google_types.GenerateContentConfig] = None,
    ) -> Tuple[object, List]:
        if not self.gemini_client:
            raise Exception("Vertex Gemini client not initialised")
        self._stats["vertex_calls"] += 1
        response = self.gemini_client.models.generate_content(
            model=model_name or ai_settings.GEMINI_MODEL_IMAGE,
            contents=contents,
            config=config or self._get_vertex_image_config(),
        )
        return response, []

    def _generate_vertex_text(self, contents: list) -> Tuple[object, List]:
        return self._generate_vertex(
            contents=contents,
            model_name=ai_settings.GEMINI_MODEL_ANALYSIS,
            config=self._get_vertex_text_config(),
        )

    def _validate_openrouter_image_response(self, response) -> None:
        try:
            self._extract_image_bytes(response)
        except Exception as exc:
            debug_summary = self._summarize_response_for_debug(response)
            print(
                f"⚠️  OpenRouter non-image output → Vertex fallback. Summary: {debug_summary}"
            )
            raise Exception(str(exc))

    def _generate_with_fallback(
        self,
        contents:  list,
        *,
        text_only: bool = False,
        openrouter_model: Optional[str] = None,
    ) -> Tuple[object, List, Dict[str, str]]:
        """Generate via OpenRouter, falling back to Vertex Gemini.

        `openrouter_model` lets one call site prefer a non-default OpenRouter
        image model (used by the isometric / bird's-eye render). The preferred
        model is tried first; if it fails for ANY reason the pipeline's default
        OpenRouter model is tried next, and only then the existing
        error-gated Vertex Gemini fallback runs. Callers that pass nothing keep
        the original single-model behaviour untouched.
        """
        warnings: List[str] = []

        default_openrouter_model = self._get_provider_model(
            Provider.OPENROUTER.value, text_only=text_only
        )
        openrouter_chain: List[str] = [default_openrouter_model]
        # Text analysis keeps its own dedicated model, so the override is
        # image-generation only.
        if openrouter_model and not text_only and openrouter_model != default_openrouter_model:
            openrouter_chain.insert(0, openrouter_model)

        if self.client:
            for index, model_name in enumerate(openrouter_chain):
                is_last_openrouter_attempt = index == len(openrouter_chain) - 1
                try:
                    with log_llm_call(
                        provider="OpenRouter",
                        model=model_name,
                        api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
                        operation="text analysis" if text_only else "image generation",
                    ):
                        if text_only:
                            response, _ = self._generate_openrouter_text(contents)
                        else:
                            response, _ = self._generate_openrouter(contents, model_name=model_name)
                            self._validate_openrouter_image_response(response)
                    source_info = self._build_source_info(
                        Provider.OPENROUTER.value,
                        text_only=text_only,
                        model_name=model_name,
                    )
                    return response, warnings, source_info
                except Exception as exc:
                    error_str = str(exc)
                    if not is_last_openrouter_attempt:
                        next_model = openrouter_chain[index + 1]
                        msg = (
                            f"OpenRouter model {model_name} failed; "
                            f"retrying with {next_model}."
                        )
                        warnings.append(msg)
                        print(f"⚠️  {msg} ({error_str})")
                        continue
                    can_fallback = self.gemini_client and self._should_fallback_to_vertex(error_str)
                    if can_fallback:
                        warnings.append("OpenRouter failed; falling back to Vertex Gemini.")
                        self._stats["vertex_fallbacks"] += 1
                        print(f"⚠️  OpenRouter failed → Vertex fallback: {error_str}")
                    else:
                        raise
        elif not self.gemini_client:
            raise Exception("No rendering provider is initialised")

        vertex_model = self._get_provider_model(
            Provider.GEMINI_VERTEX.value, text_only=text_only
        )
        with log_llm_call(
            provider="Vertex Gemini",
            model=vertex_model,
            api=(
                f"vertexai://{ai_settings.GOOGLE_CLOUD_PROJECT}/"
                f"{ai_settings.GOOGLE_CLOUD_LOCATION}"
            ),
            operation="text analysis fallback" if text_only else "image generation fallback",
        ):
            if text_only:
                response, _ = self._generate_vertex_text(contents)
            else:
                response, _ = self._generate_vertex(contents)
        source_info = self._build_source_info(Provider.GEMINI_VERTEX.value, text_only=text_only)
        return response, warnings, source_info

    # ───────────────────────────────────────────────────────────
    # RESPONSE PARSING
    # ───────────────────────────────────────────────────────────

    def _response_to_dict(self, response) -> Dict:
        if response is None:
            return {}
        if isinstance(response, dict):
            return response
        if hasattr(response, "model_dump"):
            try:
                return response.model_dump()
            except Exception:
                return {}
        return {}

    def _extract_response_text(self, response) -> str:
        try:
            raw = getattr(response, "text", None)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        except Exception:
            pass
        try:
            message = response.choices[0].message
            content = getattr(message, "content", None)
            if isinstance(content, str) and content.strip():
                return content.strip()
            if isinstance(content, list):
                chunks = []
                for item in content:
                    text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
                    if text:
                        chunks.append(text)
                if chunks:
                    return "\n".join(chunks).strip()
        except Exception:
            pass

        extracted: List[str] = []
        seen: set             = set()

        def visit(node) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"text", "output_text", "content"} and isinstance(value, str):
                        candidate = value.strip()
                        if candidate and not candidate.startswith("data:image/") and candidate not in seen:
                            seen.add(candidate)
                            extracted.append(candidate)
                    elif isinstance(value, (dict, list)):
                        visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(self._response_to_dict(response))
        return "\n".join(extracted).strip()

    def _extract_json_object(self, text: str) -> Dict:
        stripped = text.strip()
        if stripped.startswith("```"):
            parts = stripped.split("```")
            if len(parts) > 1:
                stripped = parts[1]
                if stripped.startswith("json"):
                    stripped = stripped[4:]
            stripped = stripped.strip()

        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        start = stripped.find("{")
        if start != -1:
            depth = 0; end = -1; in_string = False; escape_next = False
            for i, ch in enumerate(stripped[start:], start):
                if escape_next:
                    escape_next = False; continue
                if ch == "\\" and in_string:
                    escape_next = True; continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                if not in_string:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1; break
            candidate = stripped[start:end] if end != -1 else stripped[start:]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

            salvaged: Dict = {}
            for key, val in re.findall(r'"(\w+)"\s*:\s*"([^"]*)"', candidate):
                salvaged[key] = val
            for key, val in re.findall(r'"(\w+)"\s*:\s*(true|false)', candidate):
                salvaged[key] = val == "true"
            for key in ("key_rooms", "furniture_items"):
                m = re.search(rf'"{key}"\s*:\s*\[([^\]]*)', candidate)
                if m:
                    salvaged[key] = re.findall(r'"([^"]+)"', m.group(1))
            if salvaged:
                return salvaged

        raise ValueError(f"Could not extract JSON: {text[:120]!r}")

    def _extract_image_bytes(self, response) -> bytes:
        try:
            for part in response.candidates[0].content.parts:
                if hasattr(part, "inline_data") and part.inline_data:
                    data = part.inline_data.data
                    return base64.b64decode(data) if isinstance(data, str) else data
        except Exception:
            pass

        response_data      = self._response_to_dict(response)
        image_candidates: List[Tuple[str, str]] = []

        def add(kind: str, value: Optional[str]) -> None:
            if isinstance(value, str) and value:
                image_candidates.append((kind, value))

        def visit(node) -> None:
            if isinstance(node, dict):
                add("b64", node.get("b64_json"))
                add("b64", node.get("image_base64"))
                add("b64", node.get("base64"))
                url = node.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://", "data:image/")):
                    add("url", url)
                image_url = node.get("image_url")
                if isinstance(image_url, str) and image_url.startswith(("http://", "https://", "data:image/")):
                    add("url", image_url)
                elif isinstance(image_url, dict):
                    nested = image_url.get("url")
                    if isinstance(nested, str) and nested.startswith(("http://", "https://", "data:image/")):
                        add("url", nested)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        visit(value)
            elif isinstance(node, list):
                for item in node:
                    visit(item)

        visit(response_data)

        if not image_candidates:
            text_response  = self._extract_response_text(response)
            data_url_match = re.search(r"(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)", text_response)
            if data_url_match:
                image_candidates.append(("url", data_url_match.group(1)))
            else:
                url_match = re.search(r"https?://\S+", text_response)
                if url_match:
                    image_candidates.append(("url", url_match.group(0).rstrip(").,]")))

        last_error: Optional[Exception] = None
        for kind, value in image_candidates:
            try:
                if kind == "b64":
                    return base64.b64decode(value)
                if value.startswith("data:image/"):
                    return base64.b64decode(value.split(",", 1)[1])
                r = requests.get(value, timeout=60)
                r.raise_for_status()
                return r.content
            except Exception as exc:
                last_error = exc

        if last_error:
            raise Exception(f"Failed to read image from OpenRouter response: {last_error}")
        raise Exception("No image data in OpenRouter response")

    # ───────────────────────────────────────────────────────────
    # IMAGE SAVE
    # ───────────────────────────────────────────────────────────

    def _render_bytes(
        self,
        response,
        apply_canvas_padding:  bool = True,
        composite_base:        Optional[PILImage.Image] = None,
        composite_mask:        Optional[PILImage.Image] = None,
        match_size_to:         Optional[PILImage.Image] = None,
    ) -> bytes:
        """Post-process a model response into final PNG bytes.

        Returns bytes rather than writing a file: the caller — the Django
        adapter — stores every generated image as a `ProjectAsset` under
        `media/projects/<id>/`, which is the product's only image store. This
        used to also write its own copy into a pipeline output directory, so
        each generation occupied disk twice and nothing ever cleaned the
        second copy up.
        """
        image_bytes = self._extract_image_bytes(response)

        # An in-place edit must come back at the source render's exact pixel
        # size: the model is free to answer at its own resolution/aspect, and
        # any difference reads to the user as the view having been re-framed.
        if match_size_to is not None:
            try:
                generated = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
                if generated.size != match_size_to.size:
                    generated = generated.resize(
                        match_size_to.size, resample=PILImage.Resampling.LANCZOS
                    )
                    buf = io.BytesIO()
                    generated.save(buf, format="PNG")
                    image_bytes = buf.getvalue()
            except Exception as e:
                print(f"⚠️  Size match failed (using original): {e}")

        if composite_base is not None and composite_mask is not None:
            generated = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
            base = composite_base.convert("RGB")
            if generated.size != base.size:
                generated = generated.resize(base.size, resample=PILImage.Resampling.LANCZOS)
            mask = composite_mask.convert("L")
            if mask.size != base.size:
                mask = mask.resize(base.size, resample=PILImage.Resampling.NEAREST)
            composited = PILImage.composite(generated, base, mask)
            buf = io.BytesIO()
            composited.save(buf, format="PNG")
            image_bytes = buf.getvalue()
        elif apply_canvas_padding:
            try:
                img = PILImage.open(io.BytesIO(image_bytes)).convert("RGB")
                img = CanvasProcessor.add_safe_canvas(img)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                image_bytes = buf.getvalue()
            except Exception as e:
                print(f"⚠️  Canvas padding failed (using original): {e}")

        return image_bytes

    # ───────────────────────────────────────────────────────────
    # STAGE 1 — GEOMETRY ANCHOR
    # ───────────────────────────────────────────────────────────

    def generate_base_3d(
        self,
        floor_plan_url:   str,
        geometry_data:    dict = None,
        force_regenerate: bool = False,
        use_cache:        bool = True,
        cache_key:        str  = None,
    ) -> PipelineResult:
        print("\n" + "=" * 60)
        print(f"🏗️  STAGE 1: GEOMETRY [{self._pipeline_provider_route_label()}]")
        print("=" * 60)

        self._stats["stage1_calls"] += 1

        # The Stage 1 disk cache was removed with the pipeline output
        # directory; images are no longer persisted outside media storage.
        try:
            floor_plan_img = self._load_image(floor_plan_url)
            if not floor_plan_img:
                raise ValueError(f"Failed to load floor plan: {floor_plan_url}")

            print("🔄 Generating Stage 1 geometry…")
            response, warnings, source_info = self._generate_with_fallback(
                [Prompts.STAGE_1_GEOMETRY, floor_plan_img]
            )
            self._log_generation_source("STAGE 1", source_info)

            image_bytes = self._render_bytes(
                response,
                apply_canvas_padding=True,
            )

            print("✅ Stage 1 Complete!")
            return PipelineResult(
                success=True,
                stage="stage_1_geometry",
                image_bytes=image_bytes,
                warnings=warnings,
                metadata={
                    "cached":         False,
                    "provider":       source_info["provider"],
                    "provider_label": source_info["provider_label"],
                    "model":          source_info["model"],
                    "source_label":   source_info["label"],
                },
            )

        except Exception as e:
            err_msg = str(e)
            print(f"❌ Stage 1 Failed: {err_msg}")
            return PipelineResult(
                success=False, stage="stage_1_geometry", error=self._map_error(err_msg)
            )

    # ───────────────────────────────────────────────────────────
    # STAGE 2A-1 — ANALYZE FLOOR PLAN
    # ───────────────────────────────────────────────────────────

    def _analyze_floorplan_type(self, floor_plan_img: PILImage.Image) -> Dict:
        print("  🔍 Step A: Deep Technical Scan of floor plan…")

        defaults = {
            "space_type":           "residential_apartment",
            "subtype_notes":        "unknown",
            "key_rooms":            [],
            "scale":                "medium",
            "style_recommendation": "modern",
            "has_furniture":        False,
            "furniture_coverage":   "none",
            "furniture_by_room":    {},
            "has_dimensions":       False,
            "dimension_coverage":   "none",
            "furniture_items":      [],
            "unit_system":          "imperial",
            "dominant_unit":        "ft",
            "wall_height_estimate": "9ft-0in",
            "wall_thickness_ext":   "9in",
            "wall_thickness_int":   "4.5in",
            "door_width_typical":   "3ft-0in",
            "window_width_typical": "3ft-0in",
        }

        try:
            response, warnings, source_info = self._generate_with_fallback(
                [Prompts.STAGE_2A_ANALYZE_FLOORPLAN, floor_plan_img],
                text_only=True,
            )
            self._log_generation_source("STAGE 2A", source_info)

            raw_text = self._extract_response_text(response).strip()
            analysis = self._extract_json_object(raw_text)

            for bool_key in ("has_furniture", "has_dimensions"):
                val = analysis.get(bool_key, False)
                if isinstance(val, str):
                    analysis[bool_key] = val.strip().lower() == "true"

            if warnings:
                analysis["_warnings"] = warnings
            analysis["_provider"]     = source_info["provider"]
            analysis["_source_label"] = source_info["label"]
            analysis["_model"]        = source_info["model"]

            merged = {**defaults, **analysis}
            print(
                f"  ✅ Space: {merged.get('space_type')} — {merged.get('subtype_notes')}\n"
                f"      furniture={merged.get('has_furniture')} | "
                f"dimensions={merged.get('has_dimensions')} | "
                f"units={merged.get('dominant_unit')} | "
                f"wall_h={merged.get('wall_height_estimate')}"
            )
            return merged

        except Exception as e:
            print(f"  ⚠️  Floor plan analysis failed ({e}), using defaults.")
            return defaults

    # ───────────────────────────────────────────────────────────
    # STAGE 2A-2 — DISABLED LEGACY STYLE EXTRACTION PROMPT
    # ───────────────────────────────────────────────────────────

    def _extract_style_from_reference(self, ref_img: PILImage.Image) -> StyleSpec:
        """
        Disabled compatibility method.

        This pipeline never extracts style from reference images. The method remains
        only so older callers do not crash, but it always returns the approved
        universal StyleSpec without making any API/model call.
        """
        print("  🎨 Style extraction disabled — using universal locked StyleSpec.")
        return StyleSpec()

    # ───────────────────────────────────────────────────────────
    # MANDATE BUILDERS
    # ───────────────────────────────────────────────────────────

    def _build_furniture_mandate(self, analysis: Dict) -> str:
        coverage        = analysis.get("furniture_coverage", "none")
        furniture_items = analysis.get("furniture_items", [])
        furniture_by_room: Dict = analysis.get("furniture_by_room", {})

        if coverage not in ("full", "partial", "none"):
            coverage = "partial" if analysis.get("has_furniture", False) else "none"

        if coverage == "none":
            return Prompts._ZERO_FURNITURE_MANDATE

        if coverage == "full":
            items_str = ", ".join(furniture_items) if furniture_items else "see floor plan symbols"
            return Prompts._FURNITURE_MIRRORING_MANDATE.format(furniture_list=items_str)

        # Partial — room-by-room explicit table
        all_rooms:      List[str] = analysis.get("key_rooms", [])
        furnished_names           = list(furniture_by_room.keys())
        empty_names               = [r for r in all_rooms if r not in furnished_names]

        if not furnished_names:
            return Prompts._ZERO_FURNITURE_MANDATE

        # Build a clear room-by-room table
        room_lines = []
        for room, items in furniture_by_room.items():
            room_short  = f"{len(room_lines) + 1:02d}"
            action_str  = f"FURNISHED — replace slabs only where grey slabs exist"[:60]
            room_lines.append(f"║ {room_short:<18} ║ {action_str:<49}║\n")
        for room in empty_names:
            room_short = f"{len(room_lines) + 1:02d}"
            room_lines.append(f"║ {room_short:<18} ║ {'EMPTY — ZERO objects on floor':<49}║\n")

        room_table = "".join(room_lines) if room_lines else "║ 01                 ║ Follow grey slabs only                          ║\n"

        return Prompts._PARTIAL_FURNITURE_MANDATE.format(room_table=room_table)

    def _build_measurement_mandate(self, analysis: Dict) -> str:
        """
        Build the dimension-only annotation rules.

        v3 requirement implemented here:
        - Preserve any numeric size/dimension already written in the source plan exactly.
        - For every enclosed/usable area that has no written size, estimate L×W from the
          plan scale/proportions and mark it with (est.).
        - Never render room/function names; visible text must be numeric dimensions only.
        """
        coverage   = analysis.get("dimension_coverage", "none")
        unit_sys   = analysis.get("unit_system",         "metric")
        dominant_u = analysis.get("dominant_unit",        "m")
        wall_h     = analysis.get("wall_height_estimate", "2.80m")
        wall_ext   = analysis.get("wall_thickness_ext",   "200mm")
        wall_int   = analysis.get("wall_thickness_int",   "100mm")
        door_w     = analysis.get("door_width_typical",   "0.90m")
        win_w      = analysis.get("window_width_typical", "1.20m")
        space_type = analysis.get("space_type",           "residential_apartment")

        key_rooms = analysis.get("key_rooms", []) or []
        all_areas = analysis.get("all_enclosed_or_usable_areas", []) or []
        areas_with_dimensions = analysis.get("areas_with_dimensions", []) or []
        areas_missing_dimensions = analysis.get("areas_missing_dimensions", []) or []

        if coverage not in ("full", "partial", "none"):
            coverage = "partial" if analysis.get("has_dimensions", False) else "none"
        if unit_sys in ("unknown", ""):
            unit_sys = "metric"
        if dominant_u in ("unknown", ""):
            dominant_u = "m"

        unit_note = (
            f"UNIT SYSTEM: {unit_sys.upper()} — use {dominant_u} for ALL new estimated values. "
            "NEVER mix units or leave a value blank."
        )

        # Internal estimation hints only. These words are for sizing logic only;
        # they must NEVER be rendered as visible labels in the output image.
        _METRIC_RESIDENTIAL = {
            "bedroom": "3.50 × 3.00 m", "schlafzimmer": "3.50 × 3.00 m",
            "zimmer": "4.00 × 3.50 m", "living": "5.00 × 4.00 m",
            "wohnzimmer": "5.00 × 4.00 m", "hall": "4.50 × 3.50 m",
            "lobby": "4.00 × 3.00 m", "kitchen": "3.00 × 2.50 m", "küche": "3.00 × 2.50 m",
            "bathroom": "2.50 × 1.80 m", "bad": "2.50 × 1.80 m",
            "toilet": "1.80 × 1.20 m", "balcony": "3.00 × 1.50 m",
            "balkon": "3.00 × 1.50 m", "corridor": "2.50 × 1.20 m", "passage": "2.50 × 1.20 m",
            "flur": "2.50 × 1.20 m", "stair": "3.00 × 2.20 m", "stairs": "3.00 × 2.20 m",
            "landing": "2.20 × 1.80 m", "porch": "3.00 × 2.00 m", "car porch": "5.50 × 3.00 m",
            "carporch": "5.50 × 3.00 m", "car park": "5.50 × 3.00 m", "garage": "5.50 × 3.00 m",
            "terrace": "3.50 × 2.00 m", "lawn": "4.00 × 3.00 m", "utility": "2.00 × 1.50 m",
            "store": "2.00 × 1.50 m", "wardrobe": "2.00 × 0.80 m", "default": "3.00 × 2.50 m",
        }
        _IMPERIAL_RESIDENTIAL = {
            "bedroom": "12'-0\" × 10'-0\"", "schlafzimmer": "12'-0\" × 10'-0\"",
            "zimmer": "13'-0\" × 11'-0\"", "living": "16'-0\" × 13'-0\"",
            "wohnzimmer": "16'-0\" × 13'-0\"", "hall": "15'-0\" × 12'-0\"",
            "lobby": "13'-0\" × 10'-0\"", "kitchen": "10'-0\" × 8'-0\"", "küche": "10'-0\" × 8'-0\"",
            "bathroom": "8'-0\" × 6'-0\"", "bad": "8'-0\" × 6'-0\"",
            "toilet": "6'-0\" × 4'-0\"", "balcony": "10'-0\" × 5'-0\"",
            "balkon": "10'-0\" × 5'-0\"", "corridor": "8'-0\" × 4'-0\"", "passage": "8'-0\" × 4'-0\"",
            "flur": "8'-0\" × 4'-0\"", "stair": "10'-0\" × 7'-0\"", "stairs": "10'-0\" × 7'-0\"",
            "landing": "7'-0\" × 6'-0\"", "porch": "10'-0\" × 7'-0\"", "car porch": "18'-0\" × 10'-0\"",
            "carporch": "18'-0\" × 10'-0\"", "car park": "18'-0\" × 10'-0\"", "garage": "18'-0\" × 10'-0\"",
            "terrace": "12'-0\" × 7'-0\"", "lawn": "13'-0\" × 10'-0\"", "utility": "7'-0\" × 5'-0\"",
            "store": "7'-0\" × 5'-0\"", "wardrobe": "6'-0\" × 3'-0\"", "default": "10'-0\" × 8'-0\"",
        }
        _COMMERCIAL = {
            "bar": "15.0 × 12.0 m", "auditorium": "20.0 × 18.0 m",
            "lobby": "10.0 × 8.0 m", "corridor": "8.0 × 2.5 m", "passage": "8.0 × 2.5 m",
            "unit": "6.0 × 5.0 m", "office": "5.0 × 4.0 m", "shop": "6.0 × 5.0 m",
            "reception": "6.0 × 4.0 m", "toilet": "2.0 × 1.5 m", "bathroom": "2.5 × 1.8 m",
            "stair": "4.0 × 3.0 m", "stairs": "4.0 × 3.0 m", "landing": "3.0 × 2.0 m",
            "balcony": "4.0 × 2.0 m", "terrace": "5.0 × 3.0 m", "porch": "4.0 × 3.0 m",
            "car porch": "5.5 × 3.0 m", "carporch": "5.5 × 3.0 m", "car park": "5.5 × 3.0 m",
            "default": "8.0 × 6.0 m",
        }

        is_commercial = space_type in (
            "office", "retail_shop", "restaurant", "cafe",
            "cinema", "hotel", "clinic", "gym", "warehouse", "mixed_use",
        )
        is_imperial = unit_sys == "imperial"

        if is_commercial:
            dim_hints = _COMMERCIAL
        elif is_imperial:
            dim_hints = _IMPERIAL_RESIDENTIAL
        else:
            dim_hints = _METRIC_RESIDENTIAL

        def _hint_for_area(area: Any) -> str:
            area_lower = str(area).lower()
            matched_dim = dim_hints.get("default", "3.0 × 2.5 m")
            # Longest key first so "car porch" beats "porch".
            for key in sorted(dim_hints, key=len, reverse=True):
                if key != "default" and key in area_lower:
                    matched_dim = dim_hints[key]
                    break
            return matched_dim

        # Build a compact internal coverage table. Names in this table are explicitly
        # logic-only and must never be visible in the generated image.
        area_lines = []
        if not all_areas:
            area_coverage_block = (
                "    Analyzer did not return area list. The renderer MUST visually count every "
                "separated enclosed/usable region in IMAGE 1 and place one numeric L×W tag in each."
            )
        else:
            normalized_with = {str(x).strip().lower() for x in areas_with_dimensions}
            normalized_missing = {str(x).strip().lower() for x in areas_missing_dimensions}
            for idx, area in enumerate(all_areas[:24], start=1):
                area_name = str(area)
                low = area_name.strip().lower()
                if low in normalized_with:
                    action = "COPY EXISTING NUMERIC DIMENSION VERBATIM"
                elif low in normalized_missing or coverage in ("partial", "none"):
                    action = f"ADD NUMERIC L×W ESTIMATE ONLY, e.g. {_hint_for_area(area_name)} (est.) if no numeric size is visible"
                else:
                    action = "COPY if visible; otherwise ADD ESTIMATE (est.)"
                area_lines.append(f"    {idx}: {action}")
            area_coverage_block = "\n".join(area_lines)

        missing_lines = []
        for idx, area in enumerate((areas_missing_dimensions or [])[:20], start=1):
            missing_lines.append(f"    {idx}: add {_hint_for_area(area)} (est.) only; render no name")
        missing_block = "\n".join(missing_lines) if missing_lines else (
            "    Detect missing-size zones visually from separated floor regions only. "
            "Add L×W (est.) to each if no numeric size exists. Render numbers only."
        )

        if coverage == "full":
            sourcing_note = (
                "SOURCING: FULL — copy ALL existing numeric dimension values VERBATIM from IMAGE 1.\n"
                "Still perform a coverage pass: if any enclosed/usable area has no numeric L×W tag, "
                "add an estimated value and mark (est.). Do NOT copy room/function names."
            )
        elif coverage == "partial":
            sourcing_note = (
                "SOURCING: PARTIAL — copy existing numeric dimension labels exactly from IMAGE 1 floor plan.\n"
                "For every area without a numeric dimension, estimate its L×W from plan scale/proportions "
                "and mark (est.). Do NOT copy room/function names."
            )
        else:
            sourcing_note = (
                "SOURCING: NONE — no usable numeric dimension labels exist in the floor plan.\n"
                "Estimate every enclosed/usable area from proportions and mark every L×W value (est.).\n"
                "YOU MUST STILL WRITE ACTUAL NUMBERS — do not draw blank tick marks."
            )

        mandate = (
            "\n"
            + sourcing_note + "\n"
            + unit_note + "\n"
            + """
╔══════════════════════════════════════════════════════════════════╗
║  DIMENSION-ONLY MANDATE — ABSOLUTE ZERO LABELS                  ║
╠══════════════════════════════════════════════════════════════════╣
║  Visible text may contain ONLY:                                 ║
║    ✓ digits 0–9                                                 ║
║    ✓ decimal points                                             ║
║    ✓ feet/inch marks: ' and "                                   ║
║    ✓ unit symbols: m, mm, ft, in                                ║
║    ✓ dimension separators: ×, x, -, =                           ║
║    ✓ parentheses only for (est.)                                ║
║                                                                  ║
║  Visible text may NOT contain any room/space/function word.      ║
║                                                                  ║
║  FORBIDDEN visible words/tokens:                                 ║
║    ROOM, SMALL ROOM, AREA, BAR, BAR AREA, SERVICE, SERVICE AREA, ║
║    CORRIDOR, STAIRS, STAIR, LOBBY, AUDITORIUM, HALL, LOUNGE,     ║
║    BED, BEDROOM, BED ROOM, KITCHEN, TOILET, BATHROOM, BATH,      ║
║    OFFICE, STORE, BALCONY, PORCH, TERRACE, LAWN, UTILITY,        ║
║    TOTAL, OVERALL, WIDTH, DEPTH, HEIGHT, FLOOR, PLAN, LABEL.     ║
║                                                                  ║
║  Correct examples:                                               ║
║    ✓ 9'-0" × 13'-0"                                             ║
║    ✓ 10'-0" × 7'-0" (est.)                                      ║
║    ✓ 25.00 m                                                    ║
║    ✓ 3'-0" × 4'-0" (est.)                                      ║
║                                                                  ║
║  Incorrect examples — NEVER render these:                        ║
║    ✗ Small Room 5 10'-0" × 7'-0" (est.)                         ║
║    ✗ Corridor 6'-0" × 3'-0" (est.)                              ║
║    ✗ Stairs 10'-0" × 8'-0" (est.)                               ║
║    ✗ Bar Area 30'-0" × 25'-0"                                  ║
║    ✗ Total Width 50'                                            ║
║                                                                  ║
║  If a text cluster contains words + dimensions, delete the words ║
║  and keep only the numeric dimension.                            ║
╚══════════════════════════════════════════════════════════════════╝

INTERNAL COVERAGE — LOGIC ONLY, DO NOT RENDER THESE WORDS:
            """
            + area_coverage_block
            + "\n\n"
            + "INTERNAL MISSING-SIZE ESTIMATION HINTS — LOGIC ONLY, DO NOT RENDER THESE WORDS:\n"
            + missing_block
            + "\n\n"
            + """
MICRO-AREA DIMENSION COVERAGE — ABSOLUTE RULE:
• EVERY visually separated enclosed or usable region must receive one numeric L×W size callout.
• Do not skip small areas, tiny rooms, narrow corridors, stair pockets, service spaces,
  edge rooms, irregular niches, entries, porches, balconies, or partial-width spaces.
• If the area is too small for centered text, use smaller text or a short leader line.
• Every enclosed/usable area must have exactly one readable numeric L×W tag.
• Existing numeric dimensions must be copied exactly.
• Missing dimensions must be estimated and marked with (est.).
• Do not render area names, room names, or function labels — numeric dimensions only.

DIMENSION COUNT SELF-CHECK:
• Before output, count all visually separated enclosed/usable regions in IMAGE 1.
• Then count all interior L×W dimension tags in the final output.
• The count of interior L×W dimension tags must match the count of usable/enclosed regions.
• If even one small area has no numeric L×W tag, the render is incomplete and must be regenerated.

MEASUREMENT COVERAGE ALGORITHM:
1) Scan IMAGE 1 for numeric dimensions only.
2) Strip every non-numeric word from every text cluster.
3) Copy existing numeric dimensions exactly.
4) Estimate missing L×W values and mark only with (est.).
5) Place one numeric L×W tag inside each enclosed/usable area.
6) Outer boundary dimension lines must show numbers only, no TOTAL/OVERALL/WIDTH/DEPTH words.
7) Before final output, run a visible-text audit:
   if any visible text contains alphabetic words other than allowed unit symbols or (est.), remove it.

FINAL VISIBLE TEXT FORMAT:
• Interior areas: numeric L×W only.
• Outer dimensions: numeric value only.
• Wall height / window / door callouts: numeric value only if needed.
• No labels, no names, no titles, no functional words.

THREE-TIER DIMENSION SYSTEM — ALL THREE TIERS ARE MANDATORY

TIER 1 — NUMERIC L×W ONLY:
  • Place one compact numeric L×W dimension tag centered on each enclosed/usable floor region.
  • Existing source dimension: copy exactly, without (est.).
  • Missing source dimension: estimate and append (est.).
"""
            + "  • Format — ONE line or two lines in a small WHITE-FILLED RECTANGLE with BLACK BORDER:\n"
            + "        ┌────────────────────┐\n"
            + "        │   3.50 × 3.00 m    │\n"
            + "        └────────────────────┘\n"
            + "        ┌────────────────────┐\n"
            + "        │  5'-6\" × 3'-0\"   │\n"
            + "        │       (est.)       │\n"
            + "        └────────────────────┘\n"
            + "  • The tag MUST contain actual numbers and units.\n"
            + "  • The tag MUST NOT contain any room name, area name, function word, or label word.\n"
            + "  • Dimension every enclosed/usable floor region — no separated region is skipped.\n"
            + "  • Font: clean thin BLACK architectural sans-serif. NOT bold.\n\n"
            + "TIER 2 — BUILDING FOOTPRINT DIMENSIONS (NUMBERS ONLY):\n"
            + "  • Thin black dimension lines with tick endpoints OUTSIDE the footprint,\n"
            + "    offset 15–20px beyond the outermost wall.\n"
            + "  • Numeric value only on TOP and BOTTOM edges.\n"
            + "    Numeric value only on LEFT and RIGHT edges.\n"
            + "  • EACH LINE MUST HAVE A NUMERIC VALUE — not just a tick mark.\n"
            + "    Example: '12.50 m' or '41ft-0in' written along the dimension line.\n"
            + "  • Optional wall-thickness note must be numbers only: '" + wall_ext + " / " + wall_int + "'\n"
            + "  • All dimension lines must be fully within the canvas — never clipped.\n\n"
            + "TIER 3 — COMPONENT CALLOUTS (one of each, minimal):\n"
            + "  • Wall height callout on ONE exterior face: '" + wall_h + "' with arrow.\n"
            + "  • ONE door width callout: '" + door_w + "' with leader line.\n"
            + "  • ONE window width callout: '" + win_w + "' with leader line.\n"
            + "  • Staircase: numeric rise + tread count only if present.\n\n"
            + "DIMENSION SELF-CHECK — verify before output:\n"
            + "  □ Every enclosed/usable region has a numeric L×W tag?\n"
            + "  □ Areas that already had dimensions were copied exactly?\n"
            + "  □ Areas that lacked dimensions received estimated numbers with (est.)?\n"
            + "  □ No separated floor region is skipped?\n"
            + "  □ Zero room/function/area/label words are visible anywhere?\n"
            + "  □ Every outer dimension tick mark has a NUMBER next to it?\n"
            + "  □ No blank tick marks anywhere?\n"
        )
        return mandate

    # ───────────────────────────────────────────────────────────
    # STAGE 2B — SINGLE UNIVERSAL STYLE COMPOSITOR
    # ───────────────────────────────────────────────────────────


    def _build_initial_user_instruction_block(self, prompt: Optional[str] = None) -> str:
        """
        Converts the user's initial generation prompt into a controlled Stage 2 directive.
        This lets user instructions pass through at initial generation time while protecting
        unrelated geometry, colours, labels, camera, and previously-good styling.
        """
        raw = (prompt or "").strip()

        default_floor = (
            "Default floor: light warm-grey polished concrete / microcement; "
            "subtle cloudy low-contrast material variation; matte/satin finish; "
            "no tile seams, no wood pattern, no marble veins, no carpet, no glossy reflection, "
            "no heavy/noisy shadows."
        )
        default_walls = (
            "Default walls: clean matte off-white / neutral architectural white; "
            "smooth plain surfaces; thick crisp black SketchUp-style outlines on wall tops, "
            "corners, openings, partitions, window/door frames, floor/wall junctions, and all boundaries; "
            "no wallpaper, brick, marble, decorative texture, random tint, or colour drift."
        )

        if not raw:
            return f"""
        No extra user instruction was provided for this initial render.
        Apply the approved default wall/floor theme:
        • {default_floor}
        • {default_walls}
        • Keep furniture policy from the floor plan analysis only.
        • Do not invent extra objects, labels, colours, or materials.
"""

        lowered = raw.lower()
        floor_terms = (
            "floor", "flooring", "tiles", "tile", "wood", "wooden", "marble",
            "concrete", "cement", "microcement", "carpet", "rug", "ground"
        )
        wall_terms = (
            "wall", "walls", "paint", "wallpaper", "brick", "plaster", "panel",
            "cladding", "partition", "colour", "color"
        )
        object_terms = (
            "furniture", "bed", "sofa", "chair", "table", "desk", "wardrobe",
            "cabinet", "counter", "toilet", "sink", "bathtub", "shower", "seat",
            "stool", "plant", "lamp", "decor", "fixture", "add"
        )

        mentions_floor = any(t in lowered for t in floor_terms)
        mentions_walls = any(t in lowered for t in wall_terms)
        mentions_objects = any(t in lowered for t in object_terms)

        floor_rule = (
            "User explicitly mentioned floor/flooring/material; follow the user's floor instruction exactly, "
            "but keep it clean, sharp, non-noisy, and SketchUp-style. Do not change walls unless requested."
            if mentions_floor else
            f"User did not give a floor instruction; apply this default floor exactly: {default_floor}"
        )
        wall_rule = (
            "User explicitly mentioned walls/paint/material; follow the user's wall instruction exactly, "
            "but keep wall boundaries, caps, and outlines crisp. Do not change floor unless requested."
            if mentions_walls else
            f"User did not give a wall instruction; apply this default wall style exactly: {default_walls}"
        )
        object_rule = (
            "User instruction may authorize adding/changing ONLY the explicitly requested furniture/fixtures/objects. "
            "Do not add any extra furniture based on room names or space type."
            if mentions_objects else
            "No explicit object/furniture addition was requested beyond source-plan symbols; keep normal furniture policy."
        )

        return f"""
        INITIAL USER PROMPT TEXT — pass this meaning to the model:
        "{raw}"

        APPLY-INSTRUCTION RULES:
        • Apply the user instruction only where it is explicit and relevant.
        • {floor_rule}
        • {wall_rule}
        • {object_rule}
        • Edit isolation: do NOT randomly change wall colours, toilet/bathroom colours,
          floor material, window style, labels, numeric dimensions, camera angle, lighting,
          room layout, wall positions, or unrelated elements.
        • If the user asks to add furniture, add only that requested furniture and preserve
          all default walls/floor/windows unless the user explicitly changes them.
        • If any user request conflicts with geometry lock, geometry lock wins.
        • If the user instruction is vague or unrelated, keep the approved default theme.
"""

    def apply_universal_style(
        self,
        base_geometry_path: str,
        floor_plan_url:     str,
        style:              str = "modern",
        primary_ref_url:    str = None,
        custom_ref_urls:    List[str] = None,
        project_type:       str = None,
        initial_user_prompt: Optional[str] = None,
    ) -> PipelineResult:
        """
        STAGE 2 — NO-REFERENCE UNIVERSAL STYLE COMPOSITOR.

        IMPORTANT:
        This pipeline intentionally ignores every reference image input.
        The final render must always use the single approved universal style:
            wall tops #111111 | walls #FAFAF7 | floors #E8E6E0 microcement
            windows #D7F1F7 | frames #4A4A4A | background #FFFFFF
        Residential, commercial, cinema, hotel, office, cafe, restaurant, clinic,
        warehouse, and mixed-use plans all use this same visual theme.
        """
        current_time = datetime.now().strftime("%H:%M:%S")

        print("\n" + "=" * 60)
        print(f"🎨 STAGE 2: UNIVERSAL NO-REFERENCE STYLE [{self._pipeline_provider_route_label()}]")
        print("=" * 60)

        self._stats["stage2_calls"] += 1

        try:
            base_img = self._load_image(base_geometry_path)
            if not base_img:
                raise ValueError(f"Failed to load base geometry: {base_geometry_path}")

            floor_plan_img = self._load_image(floor_plan_url)
            if not floor_plan_img:
                raise ValueError(f"Failed to load floor plan: {floor_plan_url}")

            analysis = self._analyze_floorplan_type(floor_plan_img)
            warnings: List[str] = list(analysis.pop("_warnings", []))

            if primary_ref_url or custom_ref_urls:
                warnings.append(
                    "Reference images were provided but intentionally ignored. "
                    "This pipeline uses the fixed universal no-reference style only."
                )

            space_type = project_type or analysis.get("space_type", "mixed_use")
            subtype_notes = analysis.get("subtype_notes", "")
            key_rooms: List[str] = analysis.get("key_rooms", [])
            style_recommendation = "modern_residential"  # fixed; both preset keys map to same style
            has_furniture = analysis.get("has_furniture", False)
            has_dimensions = analysis.get("has_dimensions", False)

            furniture_mandate = self._build_furniture_mandate(analysis)
            measurement_mandate = self._build_measurement_mandate(analysis)

            print(f"  📚 References: DISABLED / IGNORED")
            print(f"  🎨 Universal style: wall #FFFFFF | floor #EEEEEE | caps #111111 | glass #D7F1F7")
            print(f"  🪑 Furniture: {'MIRRORING' if has_furniture else 'ZERO FURNITURE'}")
            print(
                f"  📐 Measurements: "
                f"{'COPY EXACT' if has_dimensions else 'ESTIMATE+ADD'} "
                f"[{analysis.get('dominant_unit', 'ft')}]"
            )

            style_descriptor = Prompts.STYLE_PRESETS["modern_residential"]
            user_instruction_block = self._build_initial_user_instruction_block(initial_user_prompt)
            if initial_user_prompt and initial_user_prompt.strip():
                print("  📝 Initial user prompt forwarded into Stage 2 style compositor")
            style_prompt = Prompts.STAGE_2B_APPLY_STYLE_NO_REF.format(
                space_type=space_type,
                subtype_notes=subtype_notes,
                style_recommendation=style_recommendation,
                furniture_mandate=furniture_mandate,
                lighting_environment=Prompts.LIGHTING_ENVIRONMENT,
                measurement_mandate=measurement_mandate,
                style_descriptor=style_descriptor,
                user_instruction_block=user_instruction_block,
            )

            final_reminder = (
                "★ FINAL UNIVERSAL NO-REFERENCE REMINDER: "
                "(1) Use the fixed default theme unless the user explicitly requested otherwise: wall-top #111111, walls #FAFAF7 matte off-white, floor #E8E6E0 light warm-grey microcement, windows #D7F1F7, frames #4A4A4A, background #FFFFFF. "
                "(2) Do not use reference-image style, do not infer different commercial/residential colours, and do not recolor based on source plan. "
                "(3) Default floor = smooth continuous light warm-grey polished concrete / microcement #E8E6E0 with subtle low-contrast cloudy material variation; bright but not pure white, no speckles, no white scatter, no shadow-cloud patches, no local bright islands. "
                "(4) ZERO visible floor shadows, ZERO contact shadows, ZERO AO, ZERO halos, ZERO cloudy blobs, ZERO grey stippling, ZERO salt-and-pepper dots, ZERO scattered shadow noise, ZERO local bright/white erased patches around text or furniture. Only crisp CAD edge strokes may exist at wall/floor boundaries. "
                "(5) Wall tops are mandatory #111111 source-override caps: repaint every wall-top face black even if source/IMAGE 1 has white, grey, faint, or missing wall tops. No wall segment may be missing a black top cap. "
                "(6) Every visually separated enclosed or usable region must have a numeric L×W size; do not skip small, narrow, edge, or open usable regions. Every outer dimension tick must have a numeric value. "
                "(7) ZERO room/function labels, title text, or word-only text anywhere. Visible text must be numeric dimensions only. Strip names from combined labels. Missing area sizes must be estimated and marked (est.). "
                "(8) Dimension text = crisp black CAD overlay; no floor texture, shadow, reflection, halo, or noise may touch it. "
                "(9) Zero curved arc lines on any floor at any doorway — commercial or residential. "
                "(10) Sharpness = SketchUp style: every edge razor-crisp, glass contained within frames, furniture has hard silhouettes, wall/floor junctions are sharp. (11) Apply initial user prompt only to explicitly requested items; do not alter unrelated walls/floors/toilets/windows/labels/dimensions/camera. ★"
            )

            contents = [
                style_prompt,
                "IMAGE 1 — LAYOUT REFERENCE ONLY. Use this for room positions, wall locations, footprint shape, building rotation direction, object positions, and existing numeric dimensions only. Apply the fixed universal no-reference SketchUp style plus the controlled initial user instruction block on top of this layout:",
                base_img,
                final_reminder,
            ]

            print("  🚀 Step B: Applying universal no-reference style…")

            response, gen_warnings, source_info = self._generate_with_fallback(contents)
            warnings.extend(gen_warnings)
            self._log_generation_source("STAGE 2B", source_info)

            image_bytes = self._render_bytes(
                response,
                apply_canvas_padding=True,
            )

            self._stats["total_renders"] += 1
            print("✅ Stage 2 Complete!")

            return PipelineResult(
                success=True,
                stage="stage_2_single_universal_style",
                image_bytes=image_bytes,
                base_geometry_abs_path=base_geometry_path,
                warnings=warnings,
                references_used=[],
                metadata={
                    "style": "single_universal_no_reference",
                    "provider": source_info["provider"],
                    "provider_label": source_info["provider_label"],
                    "model": source_info["model"],
                    "source_label": source_info["label"],
                    "timestamp": current_time,
                    "space_type": space_type,
                    "subtype_notes": subtype_notes,
                    "key_rooms": key_rooms,
                    "style_recommendation": style_recommendation,
                    "reference_category": "disabled",
                    "references_disabled": True,
                    "has_furniture": has_furniture,
                    "has_dimensions": has_dimensions,
                    "initial_user_prompt": initial_user_prompt or "",
                    "default_floor_material": "light warm-grey polished concrete / microcement, subtle cloudy low-contrast texture, matte/satin finish",
                    "default_wall_material": "clean matte off-white architectural walls with crisp black SketchUp outlines",
                    "universal_palette": {
                        "wall_top": "#111111",
                        "wall": "#FAFAF7",
                        "floor": "#E8E6E0",
                        "window_glass": "#D7F1F7",
                        "window_frame": "#4A4A4A",
                        "background": "#FFFFFF",
                    },
                },
            )

        except Exception as e:
            err_msg = str(e)
            print(f"❌ Stage 2 Failed: {err_msg}")
            return PipelineResult(
                success=False,
                stage="stage_2_single_universal_style",
                error=self._map_error(err_msg),
                base_geometry_abs_path=base_geometry_path,
                references_used=[],
            )

    def apply_style_transfer(
        self,
        base_geometry_path: str,
        floor_plan_url:     str,
        style:              str = "modern",
        primary_ref_url:    str = None,
        custom_ref_urls:    List[str] = None,
        project_type:       str = None,
    ) -> PipelineResult:
        """
        Compatibility alias only.

        Older code may still call apply_style_transfer(), but there is no separate
        reference/style-transfer pipeline anymore. This simply routes into the
        single universal no-reference compositor. Any reference inputs are ignored.
        """
        return self.apply_universal_style(
            base_geometry_path=base_geometry_path,
            floor_plan_url=floor_plan_url,
            style=style,
            primary_ref_url=None,
            custom_ref_urls=None,
            project_type=project_type,
        )

    # ───────────────────────────────────────────────────────────
    # PIPELINE ENTRY POINTS
    # ───────────────────────────────────────────────────────────

    def generate_initial_render(
        self,
        floor_plan_url:         str,
        style:                  str  = "modern",
        ref_image_url:          str  = None,
        prompt:                 str  = None,
        geometry_data:          dict = None,
        skip_stage1:            bool = False,
        existing_base_path:     str  = None,
        force_regenerate_base:  bool = False,
        use_cache:              bool = True,
        **kwargs,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], List, Dict]:
        print("\n" + "█" * 60)
        print(f"🚀 PIPELINE: GENERATE_FULL [{self._pipeline_provider_route_label()}]")
        print("█" * 60)
        warnings: List[str] = []

        if skip_stage1 and existing_base_path:
            abs_base     = os.path.abspath(existing_base_path)
            stage1_result = PipelineResult(
                success=True,
                stage="stage_1_geometry",
                output_abs_path=abs_base,
                base_geometry_abs_path=abs_base,
                metadata={"cached": False, "provider": "openrouter"},
            )
        else:
            stage1_result = self.generate_base_3d(
                floor_plan_url=floor_plan_url,
                geometry_data=geometry_data,
                force_regenerate=force_regenerate_base,
                use_cache=use_cache,
            )

        if not stage1_result.success:
            raise Exception(stage1_result.error)

        warnings.extend(stage1_result.warnings)

        # No-reference pipeline: ref_image_url is intentionally ignored.
        if ref_image_url:
            warnings.append("ref_image_url was provided but ignored; universal no-reference style is always used.")

        stage2_result = self.apply_universal_style(
            base_geometry_path=stage1_result.output_abs_path,
            floor_plan_url=floor_plan_url,
            style=style,
            primary_ref_url=None,
            initial_user_prompt=prompt,
        )

        if not stage2_result.success:
            warnings.append(stage2_result.error)
            return (
                stage1_result.output_url_path,
                stage1_result.output_abs_path,
                None, None,
                warnings,
                {
                    "stage1":         stage1_result.metadata,
                    "final":          stage1_result.metadata,
                    "source_label":   stage1_result.metadata.get("source_label"),
                    "provider":       stage1_result.metadata.get("provider"),
                    "provider_label": stage1_result.metadata.get("provider_label"),
                    "model":          stage1_result.metadata.get("model"),
                },
            )

        warnings.extend(stage2_result.warnings)

        self.session_memory.record(
            floor_plan_url,
            EditHistoryEntry(
                action="generate",
                instruction=(prompt.strip() if prompt and prompt.strip() else "Initial full render generated"),
                render_abs_path=stage2_result.output_abs_path,
                render_url_path=stage2_result.output_url_path,
                timestamp=datetime.now().strftime("%H:%M:%S"),
                metadata=stage2_result.metadata,
            ),
        )

        render_metadata = {
            "stage1":                 stage1_result.metadata,
            "final":                  stage2_result.metadata,
            "source_label":           stage2_result.metadata.get("source_label"),
            "provider":               stage2_result.metadata.get("provider"),
            "provider_label":         stage2_result.metadata.get("provider_label"),
            "model":                  stage2_result.metadata.get("model"),
            "final_render_abs_path":  stage2_result.output_abs_path,
            "final_render_url_path":  stage2_result.output_url_path,
        }

        return (
            stage1_result.output_url_path,
            stage1_result.output_abs_path,
            stage2_result.output_url_path,
            stage2_result.output_abs_path,
            warnings,
            render_metadata,
        )

    def edit_render(
        self,
        current_image_url:  str,
        floor_plan_url:     str,
        style:              str  = "modern",
        technical_prompt:   str  = None,
        anchor_image_url:   str  = None,
        edited_image_url:   str  = None,
        **kwargs,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], List, Dict]:
        """
        Backward-compatible edit entry point.

        Stable edit rule:
        - If an edit instruction or selected/edited 3D image exists, use 3D-only edit mode.
        - Never pass original 2D floor plan into edit generation.
        - Never fall back to full regeneration for failed edit, because that can reintroduce
          labels/room names or alter unrelated wall/floor/toilet colors.
        """
        annotated_image_path = kwargs.get("annotated_image_path") or kwargs.get("mask_image_url")

        action = self._decide_agent_action(
            current_image_url=current_image_url,
            edited_image_url=edited_image_url,
            annotated_image_path=annotated_image_path,
            instruction=technical_prompt,
            force_regenerate=bool(kwargs.get("force_regenerate", False)),
        )

        print(f"\n🤖 AGENT DECISION: {action.value.upper()}")

        if action == AgentAction.AREA_EDIT and annotated_image_path:
            result = self.edit_area(
                current_render_path=edited_image_url or current_image_url,
                annotated_image_path=annotated_image_path,
                floor_plan_url=floor_plan_url,
                instruction=technical_prompt or "Apply only the traced/selected area edit.",
                style=style,
            )
            return (
                None, None,
                result.output_url_path,
                result.output_abs_path,
                result.warnings,
                result.metadata if result.metadata else {"error": result.error, "edit_type": "area_edit"},
            )

        if action in (AgentAction.CONVERSATIONAL_EDIT, AgentAction.STYLE_UPDATE):
            edit_instruction = (technical_prompt or "").strip()
            if not edit_instruction:
                edit_instruction = (
                    "Use the selected/current 3D render as the edit base and preserve it exactly. "
                    "Do not add labels, do not change colors, and do not use the original floor plan."
                )

            result = self.edit_by_instruction(
                current_render_path=edited_image_url or current_image_url,
                floor_plan_url=floor_plan_url,
                instruction=edit_instruction,
                style=style,
            )
            return (
                None, None,
                result.output_url_path,
                result.output_abs_path,
                result.warnings,
                result.metadata if result.metadata else {"error": result.error, "edit_type": "chat_edit"},
            )

        # Generate mode only when no current 3D edit source/instruction exists.
        return self.generate_initial_render(
            floor_plan_url=floor_plan_url,
            style=style,
            ref_image_url=None,
            prompt=technical_prompt,
            use_cache=False,
            force_regenerate_base=True,
        )


    def edit_by_instruction(
        self,
        current_render_path: str,
        floor_plan_url:      str,
        instruction:         str,
        style:               str = "modern",
    ) -> PipelineResult:
        print("\n" + "=" * 60)
        print("💬 CHAT EDIT: Strict 3D-Only Instruction Modification")
        print("=" * 60)
        print(f"  📝 Instruction: {instruction}")

        try:
            floor_plan_key = self._floor_plan_session_key(floor_plan_url)
            current_render_path = self._select_edit_source_path(
                selected_3d_path=current_render_path,
                latest_3d_path=None,
                floor_plan_url=floor_plan_url,
            )

            current_img = self._load_image(current_render_path)
            if not current_img:
                raise ValueError(
                    "No editable 3D render found after source selection. "
                    "Original 2D floor plan must not be used for edit mode."
                )

            strict_lock = self._strict_edit_scope_lock(instruction)
            edit_history = self.session_memory.history_summary(floor_plan_key)
            edit_prompt  = Prompts.STAGE_CHAT_EDIT_WITH_HISTORY.format(
                edit_history=edit_history,
                instruction=instruction,
                strict_edit_scope_lock=strict_lock,
            )

            contents = [
                edit_prompt,
                "IMAGE 1 — CURRENT/LATEST 3D RENDER ONLY. Use this as the only base. Do NOT use any original 2D floor plan.",
                current_img,
                "★ FINAL STRICT EDIT REMINDER: "
                "(1) Camera/crop/perspective = IDENTICAL to IMAGE 1. "
                "(2) Apply ONLY this current user instruction: " + (instruction or "").strip() + " "
                "(3) Do NOT add labels, room names, function names, title blocks, legends, or 2D plan annotations. "
                "(4) Do NOT change floor color/material, wall colors, toilet/bathroom/washroom wall colors, windows, dimensions, lighting, camera, layout, or unrelated rooms unless explicitly requested. "
                "(5) If the instruction says add furniture/functions/objects, add only those requested objects; do not recolor rooms or add text. "
                "(6) Preserve light warm-grey microcement floor and matte off-white walls with crisp black SketchUp outlines unless explicitly requested. "
                "(7) No shadow blobs, noisy shadows, floor speckles, mottling, white scatter, grey stippling, salt-and-pepper dots, or random patches. "
                "(8) If you changed anything not requested, regenerate and undo that unrelated change. "
                "(9) PROJECTION LOCK: return IMAGE 1's own viewpoint, not a new one. Do NOT rotate, orbit, tilt, re-frame, "
                "re-centre, zoom, or re-project the scene, and do NOT convert it into an isometric / 3-quarter / diamond / "
                "corner-on view. If IMAGE 1 looks straight-down and plan-aligned, with its outer walls parallel to the image "
                "edges, the output must look straight-down and plan-aligned too, with the building occupying the same part of "
                "the frame at the same size and the same aspect ratio. "
                "(10) A broad instruction (e.g. 'all furniture', 'everything', 'the whole place') still means an IN-PLACE edit "
                "of IMAGE 1's existing pixels — swap the named items where they already stand. It NEVER means re-render the "
                "building from scratch, and re-rendering from scratch is what loses the camera. "
                "(11) Keep every dimension line, measurement callout and annotation already visible in IMAGE 1 exactly as-is. ★",
            ]

            print("  🚀 Sending strict 3D-only chat-edit request…")
            response, warnings, source_info = self._generate_with_fallback(contents)
            self._log_generation_source("CHAT EDIT", source_info)

            # These models regenerate the whole frame, so an in-place edit
            # comes back with the rest of the render re-interpreted — camera
            # re-projected, texture smoothed, exposure shifted, dimension
            # layer re-authored with invented numbers. The text locks above
            # cannot guarantee any of that, so measure what drifted and spend
            # at most one corrective retry, then restore the untouched pixels
            # deterministically below. Fails open at every step: if a check
            # cannot be measured, the response stands as-is.
            source_signature = self._frame_signature(current_img)
            generated_img = self._image_from_response(response)
            hints = self._edit_drift_hints(current_img, generated_img, source_signature)

            if hints:
                print(f"⚠️  In-place edit drifted ({len(hints)} issue(s)); retrying once with corrections:")
                for hint in hints:
                    print(f"     • {hint.split(':')[0]}")
                retry_contents = contents + [
                    "★★★ FIDELITY CORRECTION — YOUR PREVIOUS ATTEMPT WAS REJECTED ★★★ "
                    "You changed things you were never asked to change. Start again from IMAGE 1's own pixels. "
                    "Fix each of these:\n" + "\n".join(f"  ✗ {hint}" for hint in hints) + "\n"
                    "IMAGE 1 is the reference for EVERYTHING except the requested edit. Reproduce its clarity, "
                    "texture, materials, detail, sharpness, measurements, geometry, lighting and overall visual "
                    "quality exactly, and change ONLY: " + (instruction or "").strip() + " ★★★",
                ]
                retry_response, retry_warnings, retry_source_info = self._generate_with_fallback(retry_contents)
                self._log_generation_source("CHAT EDIT (fidelity retry)", retry_source_info)
                retry_img = self._image_from_response(retry_response)
                retry_hints = self._edit_drift_hints(current_img, retry_img, source_signature)

                # Keep whichever attempt preserved more of the source.
                if retry_img is not None and len(retry_hints) <= len(hints):
                    response, source_info = retry_response, retry_source_info
                    warnings = list(warnings) + list(retry_warnings)
                    generated_img, hints = retry_img, retry_hints
                if hints:
                    warnings = list(warnings) + [
                        "The model altered details it was not asked to change "
                        f"({', '.join(h.split(':')[0].lower() for h in hints)}); "
                        "the closest attempt was kept and untouched areas were restored from the original."
                    ]

            # Deterministic restore: keep the model's pixels only where the
            # edit actually landed and take everything else straight from the
            # source, so the original clarity, texture, materials, sharpness,
            # exposure and annotations survive verbatim. This is the same
            # pixel-level guarantee edit_area gets from its traced mask.
            edit_mask = (
                self._deliberate_edit_mask(
                    current_img,
                    generated_img,
                    protect_annotation_band=not self._targets_annotations(instruction),
                )
                if generated_img is not None
                else None
            )
            if edit_mask is not None:
                print("  🔒 Restoring unedited areas from the source render.")

            image_bytes = self._render_bytes(
                response,
                # add_safe_canvas() re-crops to content and re-pads to its own
                # fill ratio, which changes the returned framing and pixel
                # size — the exact thing an in-place edit must preserve.
                # edit_area disables it for the same reason.
                apply_canvas_padding=False,
                match_size_to=current_img,
                composite_base=current_img if edit_mask is not None else None,
                composite_mask=edit_mask,
            )

            self.session_memory.record(
                floor_plan_key,
                EditHistoryEntry(
                    action="chat_edit",
                    instruction=instruction,
                    render_abs_path='',
                    render_url_path='',
                    timestamp=datetime.now().strftime("%H:%M:%S"),
                    metadata={"provider": source_info["provider"], "source_3d": current_render_path},
                ),
            )

            # Also store under raw key for compatibility with older callers.
            if floor_plan_url and floor_plan_key != floor_plan_url:
                self.session_memory.record(
                    floor_plan_url,
                    EditHistoryEntry(
                        action="chat_edit",
                        instruction=instruction,
                        render_abs_path='',
                        render_url_path='',
                        timestamp=datetime.now().strftime("%H:%M:%S"),
                        metadata={"provider": source_info["provider"], "source_3d": current_render_path},
                    ),
                )

            self._stats["total_renders"] += 1
            print("✅ Chat Edit Complete!")

            return PipelineResult(
                success=True,
                stage="chat_edit",
                image_bytes=image_bytes,
                warnings=warnings,
                metadata={
                    "instruction":    instruction,
                    "provider":       source_info["provider"],
                    "provider_label": source_info["provider_label"],
                    "model":          source_info["model"],
                    "source_label":   source_info["label"],
                    "edit_type":      "chat_edit",
                    "source_3d":      current_render_path,
                    "history_entries": len(self.session_memory.history(floor_plan_key)),
                    "strict_edit_scope": True,
                    "size_locked_to_source": True,
                    "source_preserved":  edit_mask is not None,
                    "fidelity_issues":   [hint.split(":")[0] for hint in hints],
                },
            )

        except Exception as e:
            err_msg = str(e)
            print(f"❌ Chat Edit Failed: {err_msg}")
            return PipelineResult(
                success=False,
                stage="chat_edit",
                error=self._map_error(err_msg),
                warnings=[self._map_error(err_msg)],
                metadata={"edit_type": "chat_edit", "strict_edit_scope": True},
            )


    def edit_area(
        self,
        current_render_path:  str,
        annotated_image_path: str,
        floor_plan_url:       str,
        instruction:          str,
        style:                str = "modern",
    ) -> PipelineResult:
        print("\n" + "=" * 60)
        print("🎯 AREA EDIT: Strict Targeted 3D-Only Modification")
        print("=" * 60)
        print(f"  📝 Instruction: {instruction}")

        try:
            floor_plan_key = self._floor_plan_session_key(floor_plan_url)
            current_render_path = self._select_edit_source_path(
                selected_3d_path=current_render_path,
                latest_3d_path=None,
                floor_plan_url=floor_plan_url,
            )

            current_img = self._load_image(current_render_path)
            if not current_img:
                raise ValueError(
                    "Failed to load selected/current 3D render. "
                    "Original 2D floor plan must not be used for area edit."
                )

            annotated_img = self._load_image(annotated_image_path)
            if not annotated_img:
                raise ValueError(f"Failed to load annotated image: {annotated_image_path}")

            strict_lock = self._strict_edit_scope_lock(instruction)
            edit_prompt  = Prompts.STAGE_EDIT_AREA.format(
                instruction=instruction,
                strict_edit_scope_lock=strict_lock,
            )
            region_mask = self._extract_area_edit_mask(current_img, annotated_img)
            region_guide = self._build_area_edit_region_guide(
                current_img, annotated_img, region_mask=region_mask
            )

            contents = [
                edit_prompt,
                "IMAGE 1 — CLEAN CURRENT 3D RENDER ONLY (base — no guide marks; outside traced area = pixel-perfect copy):",
                current_img,
                "IMAGE 2 — EDIT-ZONE GUIDE derived from IMAGE 1 (cyan outline + light tint = where to edit; "
                "the boundary is a rough hand-drawn gesture, not a cut line; do NOT reproduce guide marks):",
                region_guide,
                "★ FINAL STRICT AREA-EDIT REMINDER: "
                "(1) Camera/crop/perspective = IDENTICAL to IMAGE 1. "
                "(2) Zero annotation artifacts and zero cyan guide marks. "
                "(3) Only the cyan area may change, and only according to this user instruction: " + (instruction or "").strip() + " "
                "(3a) Read what is actually inside the cyan zone in IMAGE 1 and apply that instruction to those "
                "real elements; edit clipped objects as whole objects and leave nothing sliced or half-updated. "
                "(4) Outside the cyan area = pixel-perfect copy of IMAGE 1. "
                "(5) Do NOT add labels, room names, function names, title blocks, legends, or 2D plan annotations. "
                "(6) Do NOT change floor color/material, wall colors, toilet/bathroom/washroom wall colors, windows, dimensions, lighting, camera, layout, or unrelated rooms unless explicitly requested. "
                "(7) If the instruction says add furniture/functions/objects, add only those requested objects inside the cyan area; do not recolor rooms or add text. "
                "(8) Preserve light warm-grey microcement floor and matte off-white walls with crisp black SketchUp outlines unless explicitly requested. "
                "(9) No shadow blobs, floor speckles, mottling, white scatter, grey stippling, salt-and-pepper dots, scattered shadow noise, or random patches. ★",
            ]

            print("  🚀 Sending strict 3D-only area edit request…")
            response, warnings, source_info = self._generate_with_fallback(contents)
            self._log_generation_source("AREA EDIT", source_info)

            image_bytes = self._render_bytes(
                response,
                apply_canvas_padding=False,
                composite_base=current_img,
                composite_mask=region_mask,
            )

            self.session_memory.record(
                floor_plan_key,
                EditHistoryEntry(
                    action="area_edit",
                    instruction=instruction,
                    render_abs_path='',
                    render_url_path='',
                    timestamp=datetime.now().strftime("%H:%M:%S"),
                    metadata={"provider": source_info["provider"], "source_3d": current_render_path},
                ),
            )

            # Also store under raw key for compatibility with older callers.
            if floor_plan_url and floor_plan_key != floor_plan_url:
                self.session_memory.record(
                    floor_plan_url,
                    EditHistoryEntry(
                        action="area_edit",
                        instruction=instruction,
                        render_abs_path='',
                        render_url_path='',
                        timestamp=datetime.now().strftime("%H:%M:%S"),
                        metadata={"provider": source_info["provider"], "source_3d": current_render_path},
                    ),
                )

            self._stats["total_renders"] += 1
            print("✅ Area Edit Complete!")

            return PipelineResult(
                success=True,
                stage="area_edit",
                image_bytes=image_bytes,
                warnings=warnings,
                metadata={
                    "instruction":    instruction,
                    "provider":       source_info["provider"],
                    "provider_label": source_info["provider_label"],
                    "model":          source_info["model"],
                    "source_label":   source_info["label"],
                    "edit_type":      "area_edit",
                    "source_3d":      current_render_path,
                    "strict_edit_scope": True,
                    "mask_enforced": True,
                },
            )

        except Exception as e:
            err_msg = str(e)
            print(f"❌ Area Edit Failed: {err_msg}")
            return PipelineResult(
                success=False,
                stage="area_edit",
                error=self._map_error(err_msg),
                warnings=[self._map_error(err_msg)],
                metadata={"edit_type": "area_edit", "strict_edit_scope": True},
            )

    # One server-controlled prompt is used for both the "isometric" and
    # "birds_eye" UI choices.  Keeping it here (rather than accepting a short
    # frontend replacement prompt) prevents the camera and fidelity rules from
    # drifting between entry points.
    _ISOMETRIC_CAMERA_PROMPT = """
Perform a strict camera-angle-only edit of the supplied architectural 3D floor-plan image.

The input image is the sole and absolute source of truth. STRICTLY PRESERVE the exact same 3D model, exact same floor plan, exact same room layout, exact same walls, exact same furniture, exact same materials, exact same labels, and exact same rendering style.

REQUIRED VIEW TRANSFORMATION

Change the camera to a low front perspective / isometric-perspective style view.

The view should feel like a camera is floating in front of the floor plan, slightly above it, and looking inward across the plan from the bottom of the screen toward the top of the screen.

Important orientation rule:
Ignore the architectural “front/back” of the building itself.
Use screen orientation only:

bottom of the image = front / near side
top of the image = back / far side

The camera must be positioned near the bottom-center of the image, looking toward the top-center.

CAMERA / PERSPECTIVE TARGET

Create a view with these characteristics:

camera floating in front of the bottom edge of the floor plan
camera lowered significantly
camera slightly above the floor level, not high overhead
camera elevation roughly 20–30 degrees above horizontal
camera distance only as far as needed to fit the entire unchanged footprint
with comfortable margins; do not crop, stretch, or make the building tiny
viewing direction from bottom of screen toward top of screen
gentle downward tilt into the interior
clear perspective projection
the front / bottom walls must appear larger, taller, and more dominant
objects and walls should visibly recede into depth
the back / top area should appear smaller as it goes farther away
strong sense of front-to-back perspective depth
much less top-down appearance
not an exterior street-level eye view, but a floating low architectural perspective
VISUAL GOAL

The result should look like:

the camera is hovering in front of the plan
the bottom/front edge is closest to the viewer
the front walls are clearly visible in height
the plan recedes naturally toward the top
the nearer foreground appears larger
the far background appears smaller
there is a proper low-angle isometric-perspective / architectural perspective feel

This is not a top-down axonometric view.
This is not a flat orthographic isometric.
This should be a perspective view with visible depth compression from front to back.

OCCLUSION RULE

It is acceptable, and preferred, if the new lower front perspective causes:

some top/back elements to become partially hidden
some rear furniture or wall portions to be less visible
some upper areas to be obscured by nearer walls

Do not try to keep every part equally visible from above.

Prioritize the correct low front perspective over maximum top-down visibility.

ABSOLUTE PRESERVATION

Preserve exactly:

building footprint and proportions
room layout and room dimensions
wall positions, wall heights, wall thicknesses, and openings
doors, windows, staircase, balcony, and railings
furniture, fixtures, appliances, plants, and decorations
object count, scale, orientation, and placement
flooring, wall finishes, wood textures, and all material textures
colours, lighting character, shadows, and rendering style
dimension lines, arrows, labels, numbers, units, and typography

Do not add, remove, replace, redesign, restyle, resize, rotate, mirror, simplify, relocate, or reinterpret anything.

MATERIAL TRANSFER AND SURFACE CONTINUITY

Treat the source appearance as an immutable texture/material reference and
reproject it into the new view; do not generate a fresh material design.
Every floor, wall, and other continuous surface must retain its source colour,
material identity, texture scale, grain direction, contrast, and exposure.
Perspective may naturally change visibility, foreshortening, and apparent wall
height, but it must not change surface appearance.

Do not introduce white or transparent holes, erased/washed-out islands, broken
texture, speckles, stippling, salt-and-pepper noise, checkerboarding, cloudy
blotches, or random bright/dark patches. If a newly visible or ambiguous region
must be represented, extend the nearest supported source material continuously;
never fill it with white noise or invent a different texture. Reject and redo
the result if any source surface becomes patchy or locally loses its material.

STRICT NON-INVENTION RULE

Do not invent any new architectural or decorative content.

Do not add:

new walls
new furniture
new decorations
new railings
new textures
new materials
new labels
new measurements
new exterior elements
new hidden surfaces
new object details not visible in the source

If the lower perspective reveals areas that were not clearly visible before, do not creatively complete them.

Only use what is supported by the source image.
Natural occlusion is acceptable and preferred over invention.

ONLY ALLOWED CHANGE

The only allowed modification is the viewpoint:

move the camera to the bottom/front side of the screen
lower the camera significantly
make the view more frontal from the bottom edge
apply a clear perspective projection
show more wall height in the foreground
make the scene recede toward the top of the image
keep the scene itself completely unchanged
WHAT MUST NOT HAPPEN

Do not:

regenerate the architecture
redesign the floor plan
change room sizes
change wall geometry
change furniture positions
improve or beautify the design
invent plausible missing details
change colours or materials
change lighting setup
sharpen, stylize, or enhance the image
keep a top-down isometric look
use the building’s own “front” orientation instead of the screen bottom-to-top orientation
FINAL RESULT

The output must look like the exact same architectural floor-plan render, but viewed from a lower floating front perspective, where:

the bottom of the screen is the near/front side
the top of the screen is the far/back side
the front walls are larger and taller
the scene gets smaller toward the top
there is a clear architectural perspective depth
some rear/top elements may be partially hidden

Camera change only. Perspective view from screen-bottom toward screen-top. No redesign. No invention. No added details.
"""

    _ISOMETRIC_VIEW_TYPES = frozenset({"isometric", "birds_eye"})

    def generate_isometric_from_image(
        self,
        source_image_path: str,
        prompt: Optional[str] = None,
    ) -> PipelineResult:
        print("\n" + "=" * 60)
        print("📐 ISOMETRIC VIEW: Angle-Only Transformation")
        print("=" * 60)

        try:
            # ── 1. Load source ────────────────────────────────────────────
            resolved_path = self._resolve_url_to_path(source_image_path)
            source_img = self._load_image(resolved_path)
            print(f"Source: {source_image_path}")

            if not source_img:
                raise ValueError(f"Failed to load source image: {source_image_path}")

            # ── 2. Build prompt ───────────────────────────────────────────

            # `prompt` is retained in the public signature for API/backward
            # compatibility, but it must never replace the canonical camera and
            # scene-lock instructions. The selected source image is the only
            # authority for scene content.
            final_prompt = self._ISOMETRIC_CAMERA_PROMPT

            # ── 3. Generate ───────────────────────────────────────────────
            contents = [
                "SOURCE 3D IMAGE — immutable scene reference; change only its camera:",
                source_img,
                final_prompt,
            ]
            # Isometric / bird's-eye is the ONE image call that runs on the
            # OpenAI model via OpenRouter. If it fails, _generate_with_fallback
            # retries the pipeline's default OpenRouter model and then Vertex
            # Gemini, so behaviour degrades to exactly what it was before.
            response, warnings, source_info = self._generate_with_fallback(
                contents,
                openrouter_model=ai_settings.OPENROUTER_MODEL_ISOMETRIC,
            )
            self._log_generation_source("ISOMETRIC VIEW", source_info)

            # ── 4. Guard: detect unchanged output ─────────────────────────
            if self._response_matches_source(response, resolved_path):
                msg = (
                    "Output is identical to source — model did not change the angle. "
                    "Try a different provider or model."
                )
                warnings.append(msg)
                print(f"⚠️  {msg}")

            # ── 5. Save & return ──────────────────────────────────────────
            # apply_canvas_padding=False: add_safe_canvas() samples 8 border
            # points to guess the "background" colour and flattens anything
            # close to it to white. The isometric source is a full-bleed scene
            # reprojection (not a floating object on a designed white margin
            # like the guided render), so a light floor/wall can sit at those
            # sample points and get wiped to white across the whole image —
            # exactly the washed-out/white-patch artifact this pipeline must
            # avoid per _ISOMETRIC_CAMERA_PROMPT. Area-edit disables this same
            # post-process for the same reason (see edit_area below).
            image_bytes = self._render_bytes(
                response,
                apply_canvas_padding=False,
            )

            self._stats["total_renders"] += 1
            print("✅ Isometric View Complete!")

            return PipelineResult(
                success=True,
                stage="isometric_view",
                image_bytes=image_bytes,
                warnings=warnings,
                metadata={
                    "prompt": prompt,
                    "provider": source_info["provider"],
                    "provider_label": source_info["provider_label"],
                    "model": source_info["model"],
                    "source_label": source_info["label"],
                    "edit_type": "isometric_view",
                },
            )

        except Exception as e:
            err_msg = str(e)
            print(f"❌ Isometric View Failed: {err_msg}")
            return PipelineResult(
                success=False,
                stage="isometric_view",
                error=self._map_error(err_msg),
            )


    def _response_matches_source(self, response, source_path: str) -> bool:
        """Returns True if generated output is pixel-identical to source."""
        import hashlib
        from PIL import Image
        import io

        try:
            with open(source_path, "rb") as f:
                source_hash = hashlib.md5(f.read()).hexdigest()

            if isinstance(response, (bytes, bytearray)):
                response_bytes = bytes(response)
            elif isinstance(response, Image.Image):
                buf = io.BytesIO()
                response.save(buf, format="PNG")
                response_bytes = buf.getvalue()
            elif hasattr(response, "data"):
                raw = response.data
                response_bytes = bytes(raw) if isinstance(raw, (bytes, bytearray)) else None
                if not response_bytes:
                    return False
            else:
                return False

            return hashlib.md5(response_bytes).hexdigest() == source_hash

        except Exception:
            return False



    # Per-view-type prompt library
    _ANGLE_PROMPTS = {
        "isometric": None,  # None → generate_isometric_from_image uses its own built-in prompt
        # Product terminology treats bird's-eye as the same elevated oblique
        # isometric camera, never as a flat 90-degree overhead plan.
        "birds_eye": None,

        "front_elevation": (
            "Recreate this 3D render as a flat FRONT ELEVATION architectural view.\n\n"
            "Camera: placed directly in front of the main façade, perfectly orthographic (no perspective distortion).\n"
            "The image should look like a technical elevation drawing rendered in SketchUp style.\n\n"
            "WHAT MUST STAY IDENTICAL:\n"
            "- Wall colours, materials, and surface finishes\n"
            "- Window and door positions on the front face\n"
            "- SketchUp crisp black edge lines\n"
            "- Matte white/off-white walls\n\n"
            "WHAT CHANGES: Viewing angle only — flat orthographic front face, no 3D depth visible on sides.\n"
            "Clean white background. The result should look like the front face of the building drawn straight-on."
        ),

        "side_elevation": (
            "Recreate this 3D render as a flat SIDE ELEVATION architectural view.\n\n"
            "Camera: placed directly at the left (or right) side wall, perfectly orthographic (no perspective distortion).\n"
            "The image should look like a technical side-elevation drawing rendered in SketchUp style.\n\n"
            "WHAT MUST STAY IDENTICAL:\n"
            "- Wall colours, materials, and surface finishes\n"
            "- Window and door positions on the side face\n"
            "- SketchUp crisp black edge lines\n"
            "- Matte white/off-white walls\n\n"
            "WHAT CHANGES: Viewing angle only — flat orthographic side face, camera looks directly at the side wall.\n"
            "Clean white background."
        ),

        "corner_perspective": (
            "Recreate this 3D render from a CORNER PERSPECTIVE — a dramatic 3/4 angle looking INTO the space.\n\n"
            "Camera: positioned at one corner, elevated 30–40° above horizontal, aimed diagonally inward.\n"
            "Two walls and most of the floor are clearly visible. The viewer feels like standing at the corner and looking across.\n\n"
            "WHAT MUST STAY IDENTICAL:\n"
            "- Every colour, material, and surface finish — copy exactly\n"
            "- Every room's floor colour and texture\n"
            "- Every wall colour and thickness\n"
            "- Every window, door, and opening — same teal/cyan accent\n"
            "- All furniture and fixtures visible in the source\n"
            "- SketchUp-style crisp black outlines and matte surfaces\n\n"
            "WHAT CHANGES: Camera angle only — from current view → corner 3/4 perspective.\n"
            "Clean white background. No text, labels, or annotations."
        ),
    }

    def generate_angled_view(
        self,
        source_image_path: str,
        view_type: str = "isometric",
        floor_plan_url: Optional[str] = None,
        source_job_id: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> PipelineResult:
        """Generate a re-angled render from an existing 3D image using a view-type specific prompt."""
        print("\n" + "=" * 60)
        print(f"🎥 ANGLE VIEW: {view_type.upper()}")
        print("=" * 60)

        angle_prompt = prompt or self._ANGLE_PROMPTS.get(view_type)

        # Isometric and bird's-eye are aliases for the same strict 45-degree,
        # camera-only conversion. A request prompt cannot replace the canonical
        # server-side scene lock.
        if view_type in self._ISOMETRIC_VIEW_TYPES or angle_prompt is None:
            result = self.generate_isometric_from_image(
                source_image_path=source_image_path,
                prompt=prompt,
            )
        else:
            try:
                resolved_path = self._resolve_url_to_path(source_image_path)
                source_img = self._load_image(resolved_path)
                if not source_img:
                    raise ValueError(f"Failed to load source image: {source_image_path}")

                contents = [
                    "SOURCE IMAGE — base this render on the image below:",
                    source_img,
                    angle_prompt,
                    "★ FINAL REMINDER: (1) Preserve ALL materials, colours, and fixtures from the source. "
                    "(2) Only the camera angle changes. (3) SketchUp-style crisp outlines. "
                    "(4) Clean white background. (5) No text, labels, dimensions, or watermarks.",
                ]
                response, warnings, source_info = self._generate_with_fallback(contents)
                self._log_generation_source(f"ANGLE VIEW ({view_type})", source_info)

                image_bytes = self._render_bytes(
                    response,
                    apply_canvas_padding=True,
                )

                result = PipelineResult(
                    success=True,
                    stage=view_type,
                    image_bytes=image_bytes,
                    warnings=warnings,
                    metadata={
                        "view_type": view_type,
                        "provider": source_info["provider"],
                        "provider_label": source_info["provider_label"],
                        "model": source_info["model"],
                        "source_label": source_info["label"],
                        "source_job_id": source_job_id,
                    },
                )
            except Exception as e:
                err = str(e)
                print(f"❌ Angle View Failed: {err}")
                result = PipelineResult(
                    success=False,
                    stage=view_type,
                    error=self._map_error(err),
                    warnings=[self._map_error(err)],
                    metadata={"view_type": view_type},
                )

        if result.success:
            result.metadata.setdefault("source_job_id", source_job_id)
            result.metadata.setdefault("view_type", view_type)
            if floor_plan_url:
                result.metadata.setdefault("floor_plan_url", floor_plan_url)
        return result

    def generate_isometric_view(
        self,
        final_render_path: str,
        floor_plan_url: Optional[str] = None,
        source_job_id: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> PipelineResult:
        return self.generate_angled_view(
            source_image_path=final_render_path,
            view_type="isometric",
            floor_plan_url=floor_plan_url,
            source_job_id=source_job_id,
            prompt=prompt,
        )

    # ───────────────────────────────────────────────────────────
    # MISC
    # ───────────────────────────────────────────────────────────

    def generate_room(
        self,
        room_type: str,
        prompt:    str,
        style:     str = "modern",
        **kwargs,
    ) -> Tuple[Optional[str], List]:
        try:
            contents: list = [
                f"Generate a beautiful photorealistic 3D isometric interior of a {room_type}. "
                f"Style: {style}. {prompt or ''}\n"
                f"Centre the room with generous white margins on all sides. "
                f"No edge may be clipped. "
                f"Floor must use a smooth clean neutral light grey architectural material with no visible floor shadows; "
                f"avoid speckles, concrete grain, mottling, white scatter, grey stippling, salt-and-pepper dots, scattered shadow noise, cloudy shadow patches, broad soft shadows, or random patches."
            ]
            # No reference images are appended. Room renders also use the same
            # universal clean style direction through the text prompt only.

            resp, warns, source_info = self._generate_with_fallback(contents)
            self._log_generation_source("ROOM RENDER", source_info)
            image_bytes = self._render_bytes(resp, apply_canvas_padding=True)
            return image_bytes, warns
        except Exception as e:
            return None, [self._map_error(str(e))]

    def get_stats(self) -> Dict:
        return dict(self._stats)

    def generate_3d(
        self, *args, **kwargs
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], List, Dict]:
        """Alias for generate_initial_render (backward compatibility)."""
        return self.generate_initial_render(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETON
# ═══════════════════════════════════════════════════════════════

service = RenderingService(default_provider="openrouter")
