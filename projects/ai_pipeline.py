import json
import re
import tempfile
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

from django.core.files.base import ContentFile

from .models import FloorPlanVersion, ProcessingJob, ProjectAsset


@dataclass
class ImagePipelineOutput:
    content: bytes
    filename: str
    content_type: str
    metadata: dict


@dataclass
class FloorPlanAnalysisOutput:
    asset: ProjectAsset
    metadata: dict


@dataclass
class BOQPipelineOutput:
    response_text: str
    structured_data: dict


def execute_ai_job(job):
    if job.job_type == ProcessingJob.FLOOR_PLAN_ANALYZE:
        return analyze_floor_plan(job)
    if job.job_type in {
        ProcessingJob.FLOOR_PLAN_GENERATE,
        ProcessingJob.FLOOR_PLAN_EDIT,
    }:
        return generate_floor_plan(job)
    if job.job_type in {
        ProcessingJob.THREE_D_GENERATE,
        ProcessingJob.THREE_D_EDIT,
        ProcessingJob.THREE_D_ANGLE,
    }:
        return generate_three_d(job)
    if job.job_type == ProcessingJob.BOQ_GENERATE:
        return run_boq_turn(job)
    raise ValueError(f'Unsupported AI job type: {job.job_type}')


def _asset_path(asset):
    if asset is None or not asset.file:
        raise ValueError('The required project asset is missing.')
    try:
        return asset.file.path
    except NotImplementedError as exc:
        raise ValueError(
            'The preserved AI pipeline requires local or mounted file storage.'
        ) from exc


def _take_result_content(result, byte_attribute, path_attribute=None):
    """Return the generated image's bytes.

    The AI runtime hands every image back in memory and writes nothing to
    disk; this adapter is the only thing that persists one, as a
    `ProjectAsset` under `media/projects/<id>/`. `path_attribute` is accepted
    for the older call signature and ignored.
    """
    direct_content = getattr(result, byte_attribute, None)
    if isinstance(direct_content, bytes) and direct_content:
        return direct_content
    if isinstance(direct_content, list) and direct_content:
        first_content = direct_content[0]
        if isinstance(first_content, bytes) and first_content:
            return first_content
    raise RuntimeError('The AI provider returned no image output.')


def _json_safe(value):
    if is_dataclass(value):
        value = asdict(value)
    return json.loads(json.dumps(value, default=str))


def _public_metadata(value):
    value = _json_safe(value)
    if isinstance(value, dict):
        return {
            key: _public_metadata(item)
            for key, item in value.items()
            if 'path' not in key.lower()
        }
    if isinstance(value, list):
        return [_public_metadata(item) for item in value]
    if isinstance(value, str):
        return re.sub(r'/(?:app|tmp)/[^\s,;]+', '[internal path]', value)
    return value


def _image_output(result, filename, source):
    if not getattr(result, 'success', False):
        raise RuntimeError(getattr(result, 'error', None) or 'AI generation failed.')
    content = _take_result_content(result, 'render_images')
    metadata = {
        'ai_pipeline': True,
        'source': source,
        'warnings': _public_metadata(getattr(result, 'warnings', []) or []),
        'render_metadata': _public_metadata(
            getattr(result, 'render_metadata', {}) or {}
        ),
    }
    return ImagePipelineOutput(
        content=content,
        filename=filename,
        content_type='image/png',
        metadata=metadata,
    )


def _extract_floor_plan_geometry(floor_plan):
    """Run the fidelity-gated extraction and return (plan, asset metadata)."""
    from app.ai.guided_rendering import analyze_floorplan_verified_sync

    plan, fidelity_report = analyze_floorplan_verified_sync(
        _asset_path(floor_plan.image)
    )
    metadata = dict(floor_plan.image.metadata or {})
    metadata.update(
        {
            'ai_pipeline': True,
            'floor_plan_version_id': str(floor_plan.id),
            'floorplan_json': plan.model_dump_json(),
            'floorplan_data': plan.model_dump(mode='json'),
            'fidelity': (
                _json_safe(fidelity_report)
                if fidelity_report is not None
                else None
            ),
        }
    )
    return plan, metadata


def analyze_floor_plan(job):
    floor_plan = FloorPlanVersion.objects.select_related('image').get(
        id=job.parameters.get('floor_plan_version_id'),
        project=job.project,
        status=ProcessingJob.COMPLETED,
        image__isnull=False,
    )
    _, metadata = _extract_floor_plan_geometry(floor_plan)
    return FloorPlanAnalysisOutput(asset=floor_plan.image, metadata=metadata)


def _floor_plan_geometry(floor_plan):
    """Return the version's FloorPlan, extracting it on first use.

    Step 2 no longer depends on the client calling `step-2/analyze/` first. A
    render for an unanalyzed plan extracts the geometry here and caches it on
    the 2D asset, so later renders and the BOQ context reuse it.
    """
    from app.ai.floorplan_schema import FloorPlan

    stored = (floor_plan.image.metadata or {}).get('floorplan_json')
    if stored:
        return FloorPlan.model_validate_json(stored)

    plan, metadata = _extract_floor_plan_geometry(floor_plan)
    floor_plan.image.metadata = metadata
    floor_plan.image.save(update_fields=['metadata'])
    return plan


def _normalize_image(asset, output_path):
    from app.ai.image_preparation_service import prepare_image

    with asset.file.open('rb') as source_file:
        prepared = prepare_image(source_file.read())
    Path(output_path).write_bytes(prepared.png_bytes)
    return str(output_path)


def _compose_annotation(base_asset, mask_asset, output_path):
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix='kraios-annotation-input-') as directory:
        base_path = Path(directory) / 'base.png'
        _normalize_image(base_asset, base_path)

        with Image.open(base_path) as base_source:
            base = base_source.convert('RGBA')
        with mask_asset.file.open('rb') as mask_file:
            with Image.open(mask_file) as mask_source:
                mask_source.load()
                mask = mask_source.convert('RGBA')
        if mask.size != base.size:
            mask = mask.resize(base.size, Image.Resampling.LANCZOS)
        composite = Image.alpha_composite(base, mask)
        composite.convert('RGB').save(output_path, format='PNG')
    return str(output_path)


def generate_floor_plan(job):
    version = job.floor_plan_version
    reference_path = None
    with tempfile.TemporaryDirectory(prefix='kraios-floor-plan-') as directory:
        if version.parent_id and version.parent.image_id:
            if version.mask_id:
                reference_path = _compose_annotation(
                    version.parent.image,
                    version.mask,
                    Path(directory) / 'annotated-floor-plan.png',
                )
            else:
                reference_path = _normalize_image(
                    version.parent.image,
                    Path(directory) / 'reference-floor-plan.png',
                )

        from app.ai_service import generate_2d_from_prompt

        result = generate_2d_from_prompt(
            version.prompt or version.instruction,
            project_id=str(job.project_id),
            file_path=reference_path,
        )
        if not result.success:
            raise RuntimeError(result.error or '2D generation failed.')
        content = _take_result_content(result, 'image_bytes')

    return ImagePipelineOutput(
        content=content,
        filename=f'floor-plan-{job.id}.png',
        content_type='image/png',
        metadata={
            'ai_pipeline': True,
            'source': 'architect_2d',
            'reference_version_id': (
                str(version.parent_id) if version.parent_id else None
            ),
            'annotation_mask_id': (
                str(version.mask_id) if version.mask_id else None
            ),
            'warnings': _public_metadata(result.warnings or []),
            'geometry_data': _public_metadata(result.geometry_data),
        },
    )


def _render_style(version):
    if version.render_style == version.PHOTOREALISTIC:
        return 'photoreal'
    return 'sketchup'


def _snapshot_rooms(snapshot):
    rooms = (snapshot.metadata or {}).get('rooms') or []
    names = []
    for room in rooms:
        if isinstance(room, dict) and room.get('name'):
            names.append(str(room['name']))
        elif isinstance(room, str):
            names.append(room)
    return ', '.join(names)


def _polygon_area(polygon):
    area = 0.0
    for index, (x_start, y_start) in enumerate(polygon):
        x_end, y_end = polygon[(index + 1) % len(polygon)]
        area += x_start * y_end - x_end * y_start
    return abs(area) / 2


def _plan_room_overlays(plan):
    """Room footprints in the shape the render prompt's overlays expect."""
    overlays = []
    for room in plan.rooms:
        xs = [point[0] for point in room.polygon]
        ys = [point[1] for point in room.polygon]
        overlays.append(
            {
                'name': room.name,
                'nx': round(min(xs), 2),
                'ny': round(min(ys), 2),
                'width_m': round(max(xs) - min(xs), 2),
                'depth_m': round(max(ys) - min(ys), 2),
                'area_m2': round(_polygon_area(room.polygon), 2),
            }
        )
    return overlays


def _backend_snapshot(job, floor_plan, plan):
    """Render the camera-reference snapshot when the client uploaded none.

    A browser Three.js/WebGL snapshot is still used when the client posts one to
    `step-2/snapshots/`. Without it, the extracted geometry is drawn server-side
    so Step 2 works from a prompt alone.
    """
    from app.ai.floorplan_snapshot_renderer import render_floorplan_snapshot_bytes

    content = render_floorplan_snapshot_bytes(plan)
    filename = f'three-d-snapshot-{job.id}.png'
    snapshot = ProjectAsset(
        project=job.project,
        uploaded_by=job.created_by,
        kind=ProjectAsset.THREE_D_SNAPSHOT,
        original_name=filename,
        content_type='image/png',
        size=len(content),
        metadata={
            'source': 'backend_massing_snapshot',
            'floor_plan_version_id': str(floor_plan.id),
            'rooms': _plan_room_overlays(plan),
        },
    )
    snapshot.file.save(filename, ContentFile(content), save=True)
    job.parameters = {**job.parameters, 'snapshot_asset_id': str(snapshot.id)}
    job.save(update_fields=['parameters', 'updated_at'])
    return snapshot


def generate_three_d(job):
    version = job.three_d_version
    floor_plan_path = _asset_path(
        version.floor_plan.image if version.floor_plan_id else None
    )

    if job.job_type == ProcessingJob.THREE_D_GENERATE:
        plan = _floor_plan_geometry(version.floor_plan)
        snapshot_id = job.parameters.get('snapshot_asset_id')
        if snapshot_id:
            snapshot = ProjectAsset.objects.get(
                id=snapshot_id,
                project=job.project,
                kind=ProjectAsset.THREE_D_SNAPSHOT,
            )
        else:
            snapshot = _backend_snapshot(job, version.floor_plan, plan)

        reference_ids = job.parameters.get('style_reference_asset_ids') or []
        references_by_id = {
            str(asset.id): asset
            for asset in ProjectAsset.objects.filter(
                id__in=reference_ids,
                project=job.project,
            )
        }
        reference_paths = [
            _asset_path(references_by_id[str(reference_id)])
            for reference_id in reference_ids
        ]
        description = '\n\n'.join(
            part.strip()
            for part in (plan.description, version.prompt_message.content)
            if part and part.strip()
        )

        from app.ai.guided_rendering import generate_guided_render_checked

        result = generate_guided_render_checked(
            project_id=str(job.project_id),
            snapshot_path=_asset_path(snapshot),
            plan_path=floor_plan_path,
            rooms=_snapshot_rooms(snapshot),
            description=description,
            mode=_render_style(version),
            style=version.get_render_style_display(),
            user_style_reference_paths=reference_paths,
            room_overlays=(snapshot.metadata or {}).get('rooms') or [],
            floorplan_json=plan.model_dump_json(),
        )
        return _image_output(result, f'three-d-{job.id}.png', 'guided_render')

    if job.job_type == ProcessingJob.THREE_D_EDIT:
        if version.parent is None or version.parent.image is None:
            raise ValueError('A completed 3D source image is required for editing.')

        from app.ai_service import edit_3d_area, edit_3d_chat

        if version.mask_id:
            with tempfile.TemporaryDirectory(prefix='kraios-three-d-edit-') as directory:
                annotated_path = _compose_annotation(
                    version.parent.image,
                    version.mask,
                    Path(directory) / 'annotated-render.png',
                )
                result = edit_3d_area(
                    current_render_url=_asset_path(version.parent.image),
                    annotated_image_url=annotated_path,
                    floor_plan_path=floor_plan_path,
                    instruction=version.instruction,
                    style=_render_style(version),
                )
        else:
            result = edit_3d_chat(
                current_render_url=_asset_path(version.parent.image),
                floor_plan_url=floor_plan_path,
                instruction=version.instruction,
                style=_render_style(version),
            )
        return _image_output(result, f'three-d-edit-{job.id}.png', 'render_edit')

    if version.parent is None or version.parent.image is None:
        raise ValueError('A completed 3D source image is required for an angle view.')
    if version.angle != version.ISOMETRIC_45:
        raise ValueError('Only the isometric 45-degree generated view is supported.')

    from app.ai.rendering import service as rendering_service

    result = rendering_service.generate_angled_view(
        source_image_path=_asset_path(version.parent.image),
        view_type='isometric',
        floor_plan_url=floor_plan_path,
        source_job_id=str(job.id),
    )
    if not result.success:
        raise RuntimeError(result.error or '3D angle generation failed.')
    content = _take_result_content(result, 'image_bytes')
    return ImagePipelineOutput(
        content=content,
        filename=f'three-d-isometric-{job.id}.png',
        content_type='image/png',
        metadata={
            'ai_pipeline': True,
            'source': 'isometric_angle',
            'warnings': _public_metadata(result.warnings or []),
            'render_metadata': _public_metadata(result.metadata or {}),
        },
    )


def _staged_agent_file(asset):
    """Copy an asset into the BOQ agent's upload directory and return its path.

    The agent's document tools fall back to resolving a bare filename against
    `<AI_SCRATCH_DIR>/uploads`, which Django's media storage is not. Staging the
    file there makes both the absolute path and the filename resolvable.
    """
    from app.ai.config import ai_settings

    upload_dir = Path(ai_settings.SCRATCH_DIR) / 'uploads'
    upload_dir.mkdir(parents=True, exist_ok=True)
    staged = upload_dir / f'{asset.id}-{Path(asset.original_name).name}'
    if not staged.exists():
        with asset.file.open('rb') as source_file:
            staged.write_bytes(source_file.read())
    return staged


def _extract_dxf_summary(path):
    """BOQ-relevant content from a DXF drawing.

    Block counts come first because they are the strongest takeoff signal — an
    INSERT named DOOR_900 appearing 12 times means 12 doors. Free text comes
    last since callers truncate, and losing the tail of a label list costs
    least.
    """
    try:
        from collections import Counter

        import ezdxf

        doc = ezdxf.readfile(path)
        modelspace = doc.modelspace()
        sections = []

        blocks = Counter(
            entity.dxf.name
            for entity in modelspace.query('INSERT')
            if entity.dxf.hasattr('name')
        )
        if blocks:
            sections.append(
                '[Block counts] '
                + ', '.join(f'{name} x{count}' for name, count in blocks.most_common())
            )

        layers = sorted(
            {entity.dxf.layer for entity in modelspace if entity.dxf.hasattr('layer')}
        )
        if layers:
            sections.append('[Layers in use] ' + ', '.join(layers))

        measurements = []
        for dimension in modelspace.query('DIMENSION'):
            try:
                measurements.append(round(float(dimension.get_measurement()), 1))
            except Exception:
                continue  # associative/rotated dims can refuse to measure
        if measurements:
            sections.append('[Dimensions] ' + ', '.join(str(m) for m in measurements))

        labels = []
        for entity in modelspace.query('TEXT MTEXT'):
            try:
                raw = entity.plain_text() if hasattr(entity, 'plain_text') else entity.dxf.text
            except Exception:
                continue
            text = ' '.join(str(raw or '').split())
            if text:
                labels.append(text)
        if labels:
            sections.append('[Text labels] ' + ' | '.join(labels))

        return '\n'.join(sections) or (
            'DXF opened but contained no text, blocks or dimensions.'
        )
    except Exception as exc:
        return f'Could not read this DXF file (it may be corrupt or an unsupported version): {exc}'


def _extract_excel_summary(path):
    """Cell contents of an .xlsx workbook as pipe-separated rows."""
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        lines = []
        for sheet in workbook.worksheets:
            lines.append(f'[Sheet: {sheet.title}]')
            for row in sheet.iter_rows(values_only=True):
                cells = [str(cell) for cell in row if cell is not None]
                if cells:
                    lines.append(' | '.join(cells))
        workbook.close()
        return '\n'.join(lines)
    except Exception as exc:
        return (
            'Could not read this Excel file (legacy .xls files should be '
            f'converted to .xlsx): {exc}'
        )


def _boq_room_summary(floor_plan):
    """Room names and areas extracted from the approved 2D plan, if analyzed."""
    plan_data = (floor_plan.image.metadata or {}).get('floorplan_data') or {}
    lines = []
    for room in plan_data.get('rooms') or []:
        polygon = room.get('polygon') or []
        if len(polygon) < 3:
            continue
        area = _polygon_area(polygon)
        lines.append(f"- {room.get('name') or 'Room'}: {area:.2f} m2")
    return lines


def _boq_project_context(job):
    project = job.project
    lines = [
        f'Project ID: {project.id}',
        f'Project name: {project.name}',
    ]
    floor_plan = project.selected_floor_plan
    if floor_plan and floor_plan.image_id:
        lines.append(
            'Approved/current 2D floor plan: ' + _asset_path(floor_plan.image)
        )
        room_summary = _boq_room_summary(floor_plan)
        if room_summary:
            lines.append(
                'ROOM SUMMARY (machine-extracted from that 2D plan, in meters):'
            )
            lines.extend(room_summary)
    if project.selected_three_d and project.selected_three_d.image_id:
        lines.append(
            'Approved/current 3D render: '
            + _asset_path(project.selected_three_d.image)
        )

    documents = project.documents.select_related('asset').order_by('created_at')
    document_lines = []
    for document in documents:
        staged = _staged_agent_file(document.asset)
        document_lines.append(
            f'- Type: {document.get_document_type_display()} | '
            f'Title: {document.title} | File name: {staged.name} | '
            f'Path: {staged}'
        )
        extension = Path(document.asset.original_name).suffix.lower()
        # DXF/Excel bytes are not something the agent's own tools can read
        # (extract_text_from_pdf is PDF-only), so extract the takeoff-relevant
        # content here and hand it over as text. DWG stays reference-only —
        # ezdxf cannot open it without the ODA File Converter.
        if extension == '.dxf':
            document_lines.append(_extract_dxf_summary(staged)[:2000])
        elif extension in {'.xlsx', '.xls'}:
            document_lines.append(_extract_excel_summary(staged)[:2000])
    if document_lines:
        lines.append('UPLOADED CONTENT:')
        lines.extend(document_lines)
    return '\n'.join(lines)


# Image formats the vision models accept in a message content block.
_AGENT_IMAGE_FORMATS = {'png', 'jpeg', 'gif', 'webp'}


def _image_bytes_and_format(data):
    """Pair image bytes with the media type they actually are.

    `ProjectAsset.content_type` cannot be trusted: every AI-pipeline image
    output is stored with a hardcoded 'image/png' content_type regardless of
    what the provider actually returned, and providers reject a declared type
    that disagrees with the bytes ("specified using the image/png media type,
    but the image appears to be a image/jpeg image"). Sniff the real format
    instead, and re-encode anything the models do not accept so the
    declaration and the bytes always agree.
    """
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or '').lower()
            if fmt == 'mpo':  # multi-picture JPEG — send it as plain JPEG
                fmt = 'jpeg'
            if fmt not in _AGENT_IMAGE_FORMATS:
                buffer = io.BytesIO()
                img.convert('RGB').save(buffer, format='PNG')
                return buffer.getvalue(), 'png'
            return data, fmt
    except Exception:
        return data, 'png'


def _boq_images(job):
    conversation = job.boq_version.source_message.conversation
    user_turn_count = conversation.messages.filter(role='USER').count()
    if user_turn_count > 1:
        return None
    version = job.project.selected_three_d
    if version is None or version.image_id is None:
        return None
    with version.image.file.open('rb') as image_file:
        return [_image_bytes_and_format(image_file.read())]


def _clean_markdown_cell(value):
    return re.sub(r'[*_`]', '', value).strip()


# Matches the heading the Step 4 prompt reserves for the compiled table (see
# app/ai/boq's OUTPUT CONTRACT). Not line-anchored: the model sometimes glues
# it to the end of its prose.
_FINAL_BOQ_MARKER = re.compile(r'#{1,6}\s*final\s+boq\b', re.IGNORECASE)


def _extract_markdown_boq(response_text):
    # A Step 4 reply can show a rate-corrections diff table before the actual
    # compiled one; scanning the whole reply would save that table instead of
    # the real one. Prefer content at/after the reserved heading when present.
    marker = _FINAL_BOQ_MARKER.search(response_text)
    searchable = response_text[marker.start():] if marker else response_text
    lines = searchable.splitlines()
    for index in range(len(lines) - 2):
        header_line = lines[index].strip()
        separator_line = lines[index + 1].strip()
        if '|' not in header_line or '|' not in separator_line:
            continue
        if not re.fullmatch(r'[|:\-\s]+', separator_line):
            continue
        headers = [
            _clean_markdown_cell(cell)
            for cell in header_line.strip('|').split('|')
        ]
        normalized = [header.lower() for header in headers]
        if not any('description' in header for header in normalized):
            continue
        if not any('quantity' in header or header == 'qty' for header in normalized):
            continue

        rows = []
        for row_line in lines[index + 2 :]:
            if '|' not in row_line:
                break
            cells = [
                _clean_markdown_cell(cell)
                for cell in row_line.strip().strip('|').split('|')
            ]
            if len(cells) != len(headers):
                continue
            rows.append(dict(zip(headers, cells)))
        if rows:
            return headers, rows
    return [], []


# The BOQ table's column contract, matching the placeholder in
# `tasks.complete_boq_job` and what the client reads each row by. The agent
# titles its markdown columns however it likes from one run to the next
# ("Item No." / "Item", "Rate (AED)" / "Rate"), so its headers are mapped onto
# these before anything is stored.
BOQ_CANONICAL_COLUMNS = (
    'Item',
    'Description',
    'Quantity',
    'Unit',
    'Rate',
    'Amount',
    'Remarks',
)

_BOQ_COLUMN_SYNONYMS = {
    'item': 'Item',
    'item no': 'Item',
    'item number': 'Item',
    'sno': 'Item',
    's no': 'Item',
    'sr no': 'Item',
    'sl no': 'Item',
    'serial': 'Item',
    'serial no': 'Item',
    'description': 'Description',
    'particulars': 'Description',
    'work description': 'Description',
    'quantity': 'Quantity',
    'qty': 'Quantity',
    'unit': 'Unit',
    'units': 'Unit',
    'uom': 'Unit',
    'rate': 'Rate',
    'unit rate': 'Rate',
    'unit price': 'Rate',
    'price': 'Rate',
    'amount': 'Amount',
    'total': 'Amount',
    'total amount': 'Amount',
    'value': 'Amount',
    'remarks': 'Remarks',
    'remark': 'Remarks',
    'notes': 'Remarks',
    'note': 'Remarks',
}

# A trailing "(AED)" / "($)" on a header carries the currency, not part of the
# column's identity — captured separately so the label survives without
# breaking the key the client looks the value up by.
_BOQ_HEADER_CURRENCY = re.compile(r'\(([^)]{1,12})\)\s*$')
_CURRENCY_SYMBOLS = {'$', '€', '£', '¥', '₹', '₨', '﷼', 'AED', 'USD'}


def _canonical_boq_header(header):
    """Return (canonical column name, currency found in the header or None)."""
    text = str(header or '').strip()
    currency = None

    match = _BOQ_HEADER_CURRENCY.search(text)
    if match:
        inner = match.group(1).strip()
        if re.fullmatch(r'[A-Za-z]{3}', inner) or inner in _CURRENCY_SYMBOLS:
            currency = inner.upper() if inner.isalpha() else inner
            text = text[: match.start()].strip()

    key = re.sub(r'[^a-z0-9 ]+', ' ', text.lower())
    key = re.sub(r'\s+', ' ', key).strip()
    return _BOQ_COLUMN_SYNONYMS.get(key, text or str(header)), currency


def _canonical_boq_table(headers, rows):
    """Re-key a parsed BOQ table onto the canonical columns.

    Returns (columns, rows, currency). The agent's own naming is what made a
    compiled BOQ render with empty Rate and Amount columns: the values were
    stored under "Rate (AED)"/"Amount (AED)" while the client reads
    "Rate"/"Amount". Worse, editing a row then saved back what the client
    could see, so the real rates were lost from that version for good.

    Columns the mapping does not recognise are kept as they are rather than
    dropped — an agent that adds a column should not lose the data.
    """
    if not headers:
        return list(headers or []), list(rows or []), None

    mapping = []
    currency = None
    for header in headers:
        canonical, found = _canonical_boq_header(header)
        if found and not currency:
            currency = found
        mapping.append((header, canonical))

    canonical_rows = []
    for row in rows or []:
        if not isinstance(row, dict):
            canonical_rows.append(row)
            continue
        rebuilt = {}
        for source, canonical in mapping:
            value = row.get(source, '')
            # Two source columns can land on one canonical name; the first
            # value that actually holds something wins.
            if canonical in rebuilt and not _is_blank(value):
                if _is_blank(rebuilt[canonical]):
                    rebuilt[canonical] = value
                continue
            rebuilt.setdefault(canonical, value)
        canonical_rows.append(rebuilt)

    seen = {canonical for _, canonical in mapping}
    columns = [name for name in BOQ_CANONICAL_COLUMNS if name in seen]
    columns += [
        canonical
        for _, canonical in mapping
        if canonical not in BOQ_CANONICAL_COLUMNS and canonical not in columns
    ]
    return columns, canonical_rows, currency


def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def run_boq_turn(job):
    version = job.boq_version
    prompt = version.source_message.content

    from app.ai.boq import run_boq_agent

    response = run_boq_agent(
        message=prompt,
        session_id=f'project_{job.project_id}',
        project_context=_boq_project_context(job),
        images=_boq_images(job),
    )
    response_text = re.sub(
        r'/(?:app|tmp)/[^\s,;]+',
        '[internal path]',
        str(response),
    )
    columns, rows = _extract_markdown_boq(response_text)
    columns, rows, currency = _canonical_boq_table(columns, rows)
    structured_data = {
        'columns': columns,
        'rows': rows,
        'workflow_pending': not bool(rows),
        'agent_response': response_text,
        'session_id': f'project_{job.project_id}',
    }
    if currency:
        structured_data['currency'] = currency
    return BOQPipelineOutput(
        response_text=response_text,
        structured_data=structured_data,
    )
