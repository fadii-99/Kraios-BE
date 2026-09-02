# Kraios AI pipeline integration

This backend uses the stable AI runtime supplied in `ai_pipeline_extract.zip`.
The pristine prompts, model defaults, image ordering, fallback order, sampling
settings, fidelity gates, edit algorithms, and BOQ tools live under `app/ai/`.
The Django-specific adapter is `projects/ai_pipeline.py`.

No route, model, storage service, or frontend code was copied from the old MVP.
The old project was used only to confirm how the extracted entry points were
called.

The supplied ZIP contains an unused hard-coded credential in its original
config file. The runtime copy does not contain that value, and the ZIP is
excluded from the Docker build context. Revoke/rotate the exposed credential
and remove the archive from repository history before a production release.

## Runtime switch

`AI_PIPELINE_ENABLED` must be `True` for any real generation. With it unset or
`False`, every job takes the placeholder branch in `projects/tasks.py` and
returns a 1x1 PNG or a single "Placeholder construction item" BOQ row — useful
for frontend work without provider credits, and the first thing to check when
the product looks like "the AI is not running".

```env
AI_PIPELINE_ENABLED=True
OPENROUTER_API_KEY=...
GEMINI_API_KEY=...
OPENAI_API_KEY=...
PINECONE_API_KEY=...
PINECONE_INDEX_NAME=construction-materials
```

OpenRouter is required for floor-plan extraction, render verification, and the
BOQ agent. Gemini or Google Vertex supplies the image fallbacks. OpenAI and
Pinecone are used by BOQ tools when the user selects live pricing.

The complete optional/default variable list is in `.env.example`.

After changing dependencies or environment variables, rebuild both the web and
worker images:

```bash
sudo docker compose down
sudo docker compose up -d --build
sudo docker compose logs -f web worker
```

`ai_outputs` stores pipeline render/cache/session files shared by both workers.
`ai_state` stores the Strands multi-turn BOQ session files. Durable user-facing
outputs are always copied into normal `ProjectAsset` media storage.

## Step 2 flow

The guided renderer needs a flat-shaded 3D massing image as its camera and
geometry authority. The client can supply one, and the backend renders one when
it does not, so Step 2 works from a prompt alone:

```http
POST /api/v1/projects/{project_id}/step-2/generate/
Content-Type: application/json

{
  "prompt": "Create a furnished modern layout",
  "render_style": "SKETCHUP",
  "floor_plan_version_id": "uuid",
  "snapshot_asset_id": "uuid",
  "style_reference_asset_ids": []
}
```

Only `prompt` is required. `floor_plan_version_id` defaults to the selected (or
latest completed) 2D version, `render_style` to `SKETCHUP` — `PHOTOREALISTIC`
selects the photoreal prompt preset — and both `snapshot_asset_id` and
`style_reference_asset_ids` are optional.

## Retrying a failed generation

All three generation endpoints accept an optional `retry_message_id`:

```json
{
  "prompt": "Retry the same generation request",
  "retry_message_id": "uuid-of-the-failed-user-message"
}
```

This is supported by `step-1/generate/`, `step-2/generate/`, and
`step-3/generate/`. The id must identify the failed user message in the same
project and step conversation. The backend keeps that message, removes its
failed version and processing job, and links the replacement attempt to the
same message id. A retry of a completed, queued, or unrelated message is
rejected. Clients should replace the failed block with the returned version/job
instead of appending another user prompt block.

The worker then:

1. loads the floor plan's extracted geometry, running the fidelity-gated
   extraction and caching `floorplan_json` / `floorplan_data` on the 2D asset if
   the client never called `step-2/analyze/`;
2. uses the uploaded snapshot when `snapshot_asset_id` is set; otherwise renders
   the open-top massing shell from that geometry, saves it as a
   `THREE_D_SNAPSHOT` asset with per-room footprints, and records its id in
   `job.parameters.snapshot_asset_id`;
3. calls the preserved guided renderer with the plan's own description followed
   by the user's prompt.

The preserved model input order is prompt, massing snapshot, original CAD plan,
optional bundled reference, then user references. The verifier fails open and
performs at most one corrective retry.

### Client-rendered snapshot (optional)

A client that builds the Three.js model itself gets a closer camera match. Call
`POST .../step-2/analyze/` with `{"floor_plan_version_id": "uuid"}`, poll the
job, refetch Step 1 history for `floorplan_json`, build the model, capture a
1536 x 864 PNG after two animation frames in dollhouse/isometric view on a white
background with no grid, then upload it:

```http
POST /api/v1/projects/{project_id}/step-2/snapshots/
Content-Type: multipart/form-data

file=<webgl-snapshot.png>
floor_plan_version_id=<uuid>
rooms=[{"name":"Living Room","nx":0,"ny":0,"width_m":5,"depth_m":4,"area_m2":20}]
```

Pass the returned asset id as `snapshot_asset_id`. A snapshot from a different
floor plan is rejected.

### Edits and angles

- Text-only revision: the same generate endpoint with `original_version_id`,
  which routes to the live chat-edit function.
- Traced canvas change: `POST .../step-2/edit/` with `original_version_id`,
  `instruction` and a transparent `mask`. The adapter composites the mask over
  the clean render and calls the preserved area-edit function.
- Isometric view: `POST .../step-2/angle/` with `original_version_id`. `angle`
  defaults to `ISOMETRIC_45` and is the only accepted value, because `ORIGINAL`
  is the camera the source render already has.

## Step 1 edits

Text generation uses the extracted Architect service and its exact Vertex ->
Gemini Pro -> Gemini Flash fallback chain. A current frontend canvas mask is a
transparent annotation layer, while the extract has no separate 2D area-edit
routine. The adapter composites that annotation with the source plan and sends
the result as Architect's single reference image. The Architect implementation
itself is unchanged.

## BOQ flow

`POST /api/v1/projects/{project_id}/step-3/generate/` now represents one BOQ
agent turn. The stable agent intentionally performs only one workflow step per
request:

1. requirements analysis, then approval;
2. materials extraction, then pricing preference and approval;
3. pricing research or user prices, then approval;
4. deterministic totals and final table.

Every user prompt should repeat `POST .../step-3/generate/`. The endpoint first
returns a provisional version carrying the job so the existing poller can watch
it. For Steps 1-3, completion persists the user/assistant conversation but does
not keep an empty BOQ deliverable. Once Step 4 returns the final Markdown table,
the adapter copies it into `columns` and `rows`, keeps the completed version, and
the normal approval/download flow becomes available. Do not calculate rates or
totals again in the frontend.

Each turn is given a project context block containing the approved 2D plan and
3D render paths, the room names and areas derived from the extracted geometry,
and every uploaded document. Documents are copied into
`<AI_SCRATCH_DIR>/uploads/` first, because the agent's document tools resolve a
bare filename against that directory and Django media storage is not it. The
approved 3D render is also attached as an image on the first turn.

`ProjectDocument.document_type` includes `MEP_DRAWING`, `HVAC_DRAWING`, and
`DOOR_WINDOW_SCHEDULE` — their display labels are matched verbatim by the
agent's own prompt instructions, so do not reword them. A `.dxf` upload has
its block counts (the takeoff signal — `DOOR_900 x12` means 12 doors),
layers, dimensions and text labels extracted into the context; a `.xlsx`/
`.xls` upload has its cell contents extracted as pipe-separated rows. `.dwg`
stays reference-only (`ezdxf` cannot open it without the ODA File Converter);
the agent is told to work from its filename/type and ask for a DXF export.

The current backend uses Celery jobs plus its existing REST/Channels job status.
The extracted pipeline has no provider-host socket or polling protocol.

## Intentional compatibility changes

Only these changes were made at the extracted-code boundary:

- files were placed in the `app.ai` namespace expected by their original imports;
- the unused hard-coded credential was removed and replaced by an environment
  lookup;
- `AI_SCRATCH_DIR` is scratch space only (edit history + BOQ document staging); generated images live only in `media/`;
- BOQ imports are delayed until a worker runs a BOQ turn, so Django management
  commands do not require provider credentials at startup;
- a tiny `app.storage.get_file_url()` compatibility helper was added;
- `floorplan_snapshot_renderer` is now also the Step 2 camera-reference
  fallback, and its unused disk-writing variant was dropped.

All generation and BOQ core algorithms remain unchanged.
