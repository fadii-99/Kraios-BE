# `bim` — the BIM engine

Turns an uploaded 2D floor plan into a validated, graded **BIM plan JSON**, which
later phases build a real 3D model from.

This app is deliberately self-contained. Nothing outside it imports from it, and
it imports nothing from `projects`, `app.ai`, `accounts` (beyond the user model)
or `admin`. See [Removing it](#removing-it).

---

## What it does today

```
upload (PNG/JPEG/WebP/BMP/TIFF/PDF)
   │
   ├─ prepare        normalise to RGB PNG, flatten transparency, downscale
   │
   ├─ survey pass    what does the drawing SAY? scale, building type, room list
   │
   ├─ geometry pass  trace it — walls, openings, rooms
   │     ↳ schema repair loop, up to 2 rounds
   │
   ├─ normalise      fill omissions from per-building-type defaults,
   │                 recording every one as an assumption
   │
   ├─ grade          ~20 deterministic geometry checks, with auto-repair
   │
   ├─ audit          does the result actually resemble the drawing?
   │
   ├─ good enough?   no → retry the geometry pass with the findings
   │                 (up to 3 attempts; the BEST one is kept, not the last)
   │
   ├─ furniture pass every desk, chair, wc and basin — ONCE, on the winner
   │
   └─ grade again    the fixtures were not there the first time
```

Output: a `BimPlan` (see `schema.py`), a `QualityReport`, and an attempt record.

**Not yet built:** IFC / DWG / RVT export, and user edits. The 3D viewer exists
and is built in the browser from this plan JSON — see `frontend/src/pages/bim/`.

---

## Layout

| File | What lives there |
|---|---|
| `schema.py` | The `BimPlan` contract and per-building-type defaults. Start here. |
| `normalize.py` | Fills omitted fields before validation, recording assumptions. |
| `grading/report.py` | `Issue`, `QualityReport`, and how a score is computed. |
| `grading/checks.py` | Every rule, its tolerance, and its repair. **The tuning knobs are the constants at the top.** |
| `grading/geom.py` | Plane geometry, pure Python — no shapely. |
| `ai/config.py` | Models and budgets, all env-overridable. |
| `ai/client.py` | OpenRouter chat client (sync). |
| `ai/imaging.py` | Upload → PNG data URL. Handles PDF. |
| `ai/prompts.py` | All three passes, the repair rounds, the audit, and the worked example. |
| `ai/extractor.py` | The orchestration loop. |
| `ai/auditor.py` | The visual check. Fails open. |
| `ai/furnishing.py` | The furniture pass. Fails open. |
| `models.py` / `services.py` / `views.py` / `urls.py` / `tasks.py` | The Django surface. |

---

## API

All routes are under `/api/v1/bim/` and require an authenticated user. Every
object is scoped to `request.user`; another user's UUID answers 404.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `sources/` | The caller's floor plans, newest first, each with its latest extraction |
| `POST` | `sources/` | Upload a plan (`multipart`: `file`, optional `name`) → 201 |
| `GET` | `sources/{id}/` | One plan |
| `DELETE` | `sources/{id}/` | Delete a plan and its extractions |
| `GET` | `sources/{id}/file/` | Stream the original drawing |
| `GET` | `sources/{id}/extractions/` | Extraction history (summaries — no plan JSON) |
| `POST` | `sources/{id}/extractions/` | Start an extraction → 202 |
| `GET` | `extractions/{id}/` | Full result: plan, quality report, attempt record. **Poll this.** |

A second extraction while one is live is refused with 400 — enforced by a
partial unique index, not a check-then-insert.

`error` in a response is always the same generic sentence. The real cause is in
`BimExtraction.error` and in the logs, never on the wire.

---

## Configuration

Only `OPENROUTER_API_KEY` is required; it is already set for this project.
Everything below has a working default.

| Variable | Default | Notes |
|---|---|---|
| `BIM_MODEL_SURVEY` | `anthropic/claude-fable-5` | Reads the drawing's stated facts |
| `BIM_MODEL_GEOMETRY` | `openai/gpt-5.6-sol` | Traces it. Matches what `app/ai/config.py` already settled on for floor-plan JSON |
| `BIM_MODEL_FURNITURE` | `openai/gpt-5.6-sol` | Counts and places the furniture |
| `BIM_MAX_TOKENS_FURNITURE` | `24000` | A desk AND a chair per workstation adds up |
| `BIM_FURNITURE_ENABLED` | `true` | Fails open — no furniture, still a building |
| `BIM_MAX_FIXTURES` | `600` | Guard against one fixture per drawn line |
| `BIM_MODEL_AUDIT` | `anthropic/claude-haiku-4.5` | Fast judge; runs on every attempt |
| `BIM_MAX_TOKENS_GEOMETRY` | `32000` | A truncated answer is never parseable, so this is generous |
| `BIM_MAX_GEOMETRY_ATTEMPTS` | `3` | Full re-traces |
| `BIM_MAX_SCHEMA_REPAIRS` | `2` | Repairs of malformed JSON within one attempt |
| `BIM_MIN_ACCEPT_SCORE` | `70` | At or above this, stop retrying |
| `BIM_AUDIT_ENABLED` | `true` | Fails open when off or erroring |
| `BIM_MAX_IMAGE_DIM` | `2000` | Longest edge, in pixels |
| `BIM_MAX_UPLOAD_BYTES` | `26214400` | 25 MB |
| `BIM_PDF_RENDER_DPI` | `200` | First page only |

**Furniture is a separate pass** (`ai/furnishing.py`) because it competes with
geometry: asked for walls and a hundred repeating workstations at once, a model
spends its attention on the workstations and its wall coordinates get worse. It
runs once, on the winning attempt, and fails open.

Survey and geometry deliberately run on **different providers**: when two models
disagree about a drawing's scale, the disagreement surfaces as a grader finding
instead of as one confidently wrong answer.

---

## Grading

Two layers. The deterministic one runs first and is the reason an arbitrary
floor plan is safe to build from.

**Auto-repaired** (recorded, still costs score): near-miss corners snapped,
degenerate walls dropped, duplicate walls merged with their openings rehosted,
implausible thicknesses clamped, openings slid back inside their wall,
over-tall openings shortened, overlapping openings de-duplicated, tiny rooms
dropped, duplicate room names disambiguated, missing outlines derived, wall
heights clamped under floor-to-floor.

**Reported, never repaired** (one right answer does not exist): self-intersecting
rooms, overlapping rooms, disconnected walls, implausible door/window sizes,
low room coverage, unknown scale, absurd footprint.

Two things block acceptance regardless of score:

- an **unrepaired error** — part of the plan cannot be built;
- a **visual score ≤ 50** — the model is well-formed but is not this drawing.
  Without this rule the audit could never veto anything: at a 0.7/0.3 weighting
  a perfect-but-wrong extraction scores exactly the 70 threshold. See
  `MIN_VISUAL_SCORE` in `grading/report.py`.

Tuning lives in the constants at the top of `grading/checks.py`. If a real plan
grades badly, look there first.

---

## Tests

```bash
python -m django test bim
```

116 tests, and none of them touch the network or need a database beyond the API
suite: the grader and geometry run as `SimpleTestCase`, and every pipeline test
replaces `complete_json` in all three modules that import it. Patching only two
of them once left the furniture pass making a real HTTP request that the pass
then swallowed, because it fails open — the tests passed and went to the
provider anyway.

`test_extraction.PromptContractTests` validates the worked example inside
`ai/prompts.py` against `BimPlan`, so the contract shown to the model cannot
drift from the contract the parser enforces. Change the schema and forget the
prompt, and that test fails.

---

## Removing it

Three steps, no migration of other apps, no code changes anywhere else:

1. Delete `'bim',` from `INSTALLED_APPS` in `config/settings.py`.
2. Delete `path('api/v1/bim/', include('bim.urls')),` from `config/urls.py`.
3. Delete the `backend/bim/` directory.

Then drop the two tables it owned, `bim_bimsource` and `bim_bimextraction`, and
the `bim/` prefix under `MEDIA_ROOT`. Nothing else references either.

The frontend half removes the same way — see `frontend/src/pages/bim/README.md`.
