"""
AI Service - Real AI Integration Layer.

This module provides the interface between the backend services and the real AI modules.
Replaces the mock implementation with actual Gemini and OpenAI calls.

Entry Points:
- generate_2d_from_upload(file_path) -> Generate2DResult
- generate_2d_from_prompt(prompt) -> Generate2DResult
- refine_2d(context, prompt) -> Refine2DResult
- generate_3d(context) -> Generate3DResult
- generate_boq(context, prompt) -> GenerateBOQResult
"""

import os
from typing import Optional, List, Dict
from dataclasses import dataclass, field

from app.ai.config import ai_settings


# ============================================================================
# Data Classes for AI Results (kept for compatibility)
# ============================================================================

@dataclass
class Summary:
    """A generated summary card."""
    title: str
    text: str
    sort_order: int


@dataclass
class BOQItem:
    """A Bill of Quantities item."""
    work_item: str
    material: Optional[str]
    unit: Optional[str]
    quantity: float
    rate: float
    total: float
    currency: str = "AED"


@dataclass
class Generate2DResult:
    """Result from 2D generation."""
    image_bytes: Optional[bytes] = None
    image_url: Optional[str] = None
    file_path: Optional[str] = None
    summaries: List[Summary] = field(default_factory=list)
    geometry_data: Optional[dict] = None
    warnings: List[str] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None


@dataclass
class Refine2DResult:
    """Result from 2D refinement."""
    image_bytes: Optional[bytes] = None
    image_url: Optional[str] = None
    file_path: Optional[str] = None
    summaries: List[Summary] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None


@dataclass
class Generate3DResult:
    """Result from 3D generation."""
    render_images: List[bytes] = field(default_factory=list)
    render_urls: List[str] = field(default_factory=list)
    render_paths: List[str] = field(default_factory=list)
    stage1_url: Optional[str] = None
    stage1_path: Optional[str] = None
    render_metadata: Dict = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None


@dataclass
class GenerateBOQResult:
    """Result from BOQ generation."""
    items: List[BOQItem] = field(default_factory=list)
    grand_total: float = 0.0
    warnings: List[str] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None


@dataclass
class RoomRenderResult:
    """Result from room-specific rendering."""
    image_bytes: Optional[bytes] = None
    warnings: List[str] = field(default_factory=list)
    success: bool = True
    error: Optional[str] = None



# ============================================================================
# Error Mapping Utility
# ============================================================================

def _map_error(error_str: str) -> str:
    """Map technical AI errors to user-friendly messages for the frontend."""
    if not error_str:
        return "An unknown AI error occurred. Please try again."
        
    err_up = error_str.upper()
    
    # 429 / Quota / Exhausted
    if "429" in err_up or "RESOURCE_EXHAUSTED" in err_up or "QUOTA" in err_up:
        return "The AI service is currently at capacity. Please wait a minute and try again."
    
    # 404 / Not Found / Model issues
    if "404" in err_up or "NOT_FOUND" in err_up:
        return "The AI engine is temporarily unavailable for maintenance. Please try again in 5 minutes."
        
    # Internal / 500
    if "500" in err_up or "503" in err_up or "INTERNAL" in err_up:
        return "Our AI backend is temporarily unresponsive. We are working on a fix. Please try again shortly."
        
    # Safety
    if "SAFETY" in err_up or "CENSOR" in err_up:
        return "Your request couldn't be processed due to AI safety guardrails. Please try a different approach."
        
    # If the error already seems to be a friendly message (doesn't contain technical codes), return it
    if "STAGE 1" in err_up or "STAGE 2" in err_up:
        # Clean up internal stage labels if they leaked through
        return error_str.split(":")[-1].strip()

    return error_str


# ============================================================================
# Real AI Service Functions
# ============================================================================
def generate_2d_from_upload(file_path: str, project_id: str = "default") -> Generate2DResult:
    """
    Generate a 2D floor plan from an uploaded file.
    
    Uses Gemini to analyze the uploaded image and generate a 2D floor plan.
    Also uses the rendering service to analyze rooms and generate summaries.
    
    Args:
        file_path: Path to the uploaded file
        project_id: Project ID for file organization
        
    Returns:
        Generate2DResult with image and summaries
    """
    try:
        from app.ai.rendering import service as rendering_service
        
        # Use singleton rendering service
        
        # 1. EXTRACT GEOMETRY
        geometry_data = None
        # try:
        #     geometry_data = rendering_service.extract_geometry(file_path)
        # except Exception as e:
        #     print(f"Geometry extraction warning: {e}")

        # Analyze the uploaded floor plan to extract room descriptions
        # rooms = rendering_service.analyze_rooms(file_path)
        
        # Convert rooms to summaries
        summaries = []
        warnings = []
        # for i, room in enumerate(rooms):
        #     summaries.append(Summary(
        #         title=room.get("room_type", f"Room {i+1}"),
        #         text=room.get("description", ""),
        #         sort_order=i
        #     ))
        
        # For uploads, we use the uploaded file directly as the 2D plan
        # Read the file to get bytes for consistency
        with open(file_path, "rb") as f:
            image_bytes = f.read()
        
        # No pipeline-served URL: the caller stores this asset in media and
        # serves it through the project's asset download endpoint.
        image_url = None
        
        return Generate2DResult(
            image_bytes=image_bytes,
            image_url=image_url,
            file_path=file_path,
            summaries=summaries,
            geometry_data=geometry_data,
            success=True
        )
        
    except Exception as e:
        return Generate2DResult(
            success=False,
            error=_map_error(str(e))
        )




def generate_2d_from_prompt(prompt: str, project_id: str = "default", file_path: Optional[str] = None) -> Generate2DResult:
    """
    Generate a 2D floor plan from a text prompt (and optional image).
    
    Uses Gemini to generate a 2D architectural floor plan based on the description and optional reference image.
    
    Args:
        prompt: Text description of the desired floor plan
        project_id: Project ID for file organization
        file_path: Optional path to a reference image/file
        
    Returns:
        Generate2DResult with image and summaries
    """
    try:
        from app.ai.architect import service as architect_service
        from app.ai.rendering import service as rendering_service
        
        # Use singleton architect service
        
        # Generate 2D floor plan from prompt (and optional file)
        result, warnings = architect_service.generate_2d_plan(prompt, image_path=file_path)
        
        if not result:
            return Generate2DResult(
                success=False,
                warnings=warnings,
                error="2D generation failed"
            )
        
        # The architect returns PNG bytes; nothing is written to disk. The
        # Django adapter stores them as the project's 2D asset under media/.
        image_bytes = result
        image_url = None
        file_path = None
        
        # Now analyze the generated plan to extract room summaries
        summaries = []
        geometry_data = None
        # try:
            # Use singleton rendering service
            # Try to extract geometry 
            # try:
            #     geometry_data = rendering_service.extract_geometry(file_path)
            # except Exception as e:
            #      print(f"Geometry extraction warning: {e}")

            # rooms = rendering_service.analyze_rooms(file_path)
            # for i, room in enumerate(rooms):
            #     summaries.append(Summary(
            #         title=room.get("room_type", f"Room {i+1}"),
            #         text=room.get("description", ""),
            #         sort_order=i
            #     ))
        # except Exception as e:
        #     # If room analysis fails, create a basic summary from the prompt
        #     summaries = [
        #         Summary(
        #             title="Floor Plan",
        #             text=f"Generated floor plan based on: {prompt[:200]}...",
        #             sort_order=0
        #         )
        #     ]
        
        return Generate2DResult(
            image_bytes=image_bytes,
            image_url=image_url,
            file_path=file_path,
            summaries=summaries,
            geometry_data=geometry_data,
            success=True
        )
        
    except Exception as e:
        return Generate2DResult(
            success=False,
            error=_map_error(str(e))
        )


def refine_2d(context: dict, prompt: str, project_id: str = "default") -> Refine2DResult:
    """
    Refine an existing 2D floor plan based on feedback.
    
    Args:
        context: Current project context (summaries, etc.)
        prompt: Refinement instructions
        project_id: Project ID
        
    Returns:
        Refine2DResult with optional new image and updated summaries
    """
    try:
        from app.ai.architect import service as architect_service
        from app.ai.rendering import service as rendering_service
        
        # Build a refined prompt from context and user input
        existing_summaries = context.get("summaries", [])
        context_text = "Existing layout:\n"
        for s in existing_summaries:
            context_text += f"- {s.get('title', 'Room')}: {s.get('text', '')}\n"
        
        refined_prompt = f"{context_text}\n\nRefinement request: {prompt}"
        
        # Use singleton architect service
        result, warnings = architect_service.generate_2d_plan(refined_prompt)
        
        if not result:
            return Refine2DResult(
                success=False,
                warnings=warnings,
                error="Refinement failed"
            )
        
        # The architect returns PNG bytes; nothing is written to disk. The
        # Django adapter stores them as the project's 2D asset under media/.
        image_bytes = result
        image_url = None
        file_path = None
        
        # Analyze the refined plan
        summaries = []
        # try:
        #     # Use singleton rendering service
        #     rooms = rendering_service.analyze_rooms(file_path)
        #     for i, room in enumerate(rooms):
        #         summaries.append(Summary(
        #             title=room.get("room_type", f"Room {i+1}"),
        #             text=room.get("description", ""),
        #             sort_order=i
        #         ))
        # except:
        #     # Keep existing summaries if analysis fails
        #     for i, s in enumerate(existing_summaries):
        #         summaries.append(Summary(
        #             title=s.get("title", f"Room {i+1}"),
        #             text=s.get("text", ""),
        #             sort_order=i
        #         ))
        
        return Refine2DResult(
            image_bytes=image_bytes,
            image_url=image_url,
            file_path=file_path,
            summaries=summaries,
            warnings=warnings,
            success=True
        )
        
    except Exception as e:
        return Refine2DResult(
            success=False,
            error=_map_error(str(e))
        )





def generate_3d(context: dict, project_id: str = "default", style: str = "sketchup", prompt: Optional[str] = None) -> Generate3DResult:
    """
    Generate 3D renders from the current floor plan.
    
    Args:
        context: Current project context (must include floor_plan_path or asset_url)
        project_id: Project ID
        style: Rendering style ('sketchup' or 'cad')
        
    Returns:
        Generate3DResult with render images
    """
    try:
        from app.ai.rendering import service as rendering_service
        
        # Get the floor plan path from context
        floor_plan_path = context.get("floor_plan_path")
        if not floor_plan_path:
            floor_plan_path = context.get("asset_url")
        if not floor_plan_path:
            # Try to find the latest 2D asset
            assets = context.get("assets", [])
            for asset in assets:
                if asset.get("kind") == "GEN_2D":
                    floor_plan_path = asset.get("file_path") or asset.get("file_url")
                    break
        
        if not floor_plan_path:
            return Generate3DResult(
                success=False,
                error="No floor plan found to generate 3D from"
            )
        
        # Generate 3D render
        current_image_url = context.get("current_image_url")
        edited_image_url = context.get("edited_image_url")
        anchor_image_url = context.get("anchor_image_url")
        
        # Extract Geometry Data from context (Step 2 Logic)
        geometry_data = context.get("geometry_data")
        
        # Return signature: (s1_url, s1_path, s2_url, s2_path, warnings)
        s1_url, s1_path, s2_url, s2_path, warnings, render_metadata = (None, None, None, None, [], {})

        if (prompt and current_image_url) or edited_image_url:
            # Conversational editing flow (only if we have a previous 3D image)
            s1_url, s1_path, s2_url, s2_path, warnings, render_metadata = rendering_service.edit_render(
                current_image_url=current_image_url,
                floor_plan_url=floor_plan_path,
                style=style,
                technical_prompt=prompt,
                anchor_image_url=anchor_image_url,
                edited_image_url=edited_image_url
            )
        else:
            # Standard initial generation flow
            s1_url, s1_path, s2_url, s2_path, warnings, render_metadata = rendering_service.generate_3d(
                floor_plan_path, 
                style=style, 
                prompt=prompt, 
                geometry_data=geometry_data
            )

        if not s2_url and not s1_url:
            return Generate3DResult(
                success=False,
                warnings=warnings,
                error="3D generation failed"
            )
        
        # Read image bytes for stage 2
        render_images = []
        if s2_path and os.path.exists(s2_path):
            with open(s2_path, "rb") as f:
                render_images.append(f.read())
        
        return Generate3DResult(
            render_images=render_images,
            render_urls=[s2_url] if s2_url else [],
            render_paths=[s2_path] if s2_path else [],
            stage1_url=s1_url,
            stage1_path=s1_path,
            render_metadata=render_metadata or {},
            warnings=warnings,
            success=True
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return Generate3DResult(
            success=False,
            error=_map_error(str(e))
        )

def edit_3d_area(
    current_render_url: str,
    annotated_image_url: str,
    floor_plan_path: str,
    instruction: str,
    style: str = "modern",
) -> Generate3DResult:
    """
    Targeted area edit: Modify only interior elements within a user-traced region.

    Args:
        current_render_url: URL/path of the current 3D render (the base image)
        annotated_image_url: URL/path of the image with user's trace + label
        floor_plan_path: URL/path of the original 2D floor plan
        instruction: User's text instruction (e.g., "remove office, add cinema seats")
        style: Rendering style

    Returns:
        Generate3DResult with the edited render
    """
    try:
        from app.ai.rendering import service as rendering_service

        pipeline_result = rendering_service.edit_area(
            current_render_path=current_render_url,
            annotated_image_path=annotated_image_url,
            floor_plan_url=floor_plan_path,
            instruction=instruction,
            style=style,
        )

        if not pipeline_result.success:
            return Generate3DResult(
                success=False,
                warnings=pipeline_result.warnings,
                error=pipeline_result.error or "Area edit failed",
            )

        # The pipeline returns the finished image in memory; the Django
        # adapter stores it under media/ as the project's asset.
        render_images = (
            [pipeline_result.image_bytes] if pipeline_result.image_bytes else []
        )

        return Generate3DResult(
            render_images=render_images,
            render_metadata=pipeline_result.metadata or {},
            warnings=pipeline_result.warnings,
            success=True,
        )

    except Exception as e:
        return Generate3DResult(
            success=False,
            error=_map_error(str(e)),
        )



def edit_3d_chat(
    current_render_url: str,
    floor_plan_url: str,
    instruction: str,
    style: str = "modern",
) -> Generate3DResult:
    """
    Apply a chat instruction to an existing 3D render without regenerating from scratch.

    Args:
        current_render_url: URL/path of the current 3D render (the base image)
        floor_plan_url: URL/path of the original 2D floor plan (used for session keying)
        instruction: User's text instruction
        style: Rendering style

    Returns:
        Generate3DResult with the edited render
    """
    try:
        from app.ai.rendering import service as rendering_service

        pipeline_result = rendering_service.edit_by_instruction(
            current_render_path=current_render_url,
            floor_plan_url=floor_plan_url,
            instruction=instruction,
            style=style,
        )

        if not pipeline_result.success:
            return Generate3DResult(
                success=False,
                warnings=pipeline_result.warnings,
                error=pipeline_result.error or "Chat edit failed",
            )

        render_images = (
            [pipeline_result.image_bytes] if pipeline_result.image_bytes else []
        )

        render_metadata = pipeline_result.metadata or {}
        render_metadata.setdefault("render_source_label", "Chat Edit")
        render_metadata.setdefault("source_label", "chat_edit")

        return Generate3DResult(
            render_images=render_images,
            render_metadata=render_metadata,
            warnings=pipeline_result.warnings,
            success=True,
        )

    except Exception as e:
        return Generate3DResult(
            success=False,
            error=_map_error(str(e)),
        )


def generate_boq(context: dict, prompt: Optional[str] = None, project_id: str = "default", image_urls: Optional[list[str]] = None) -> GenerateBOQResult:
    """
    Generate Bill of Quantities for the floor plan.
    
    Args:
        context: Current project context (should include summaries)
        prompt: Optional prompt for customization
        project_id: Project ID
        image_urls: Optional list of image URLs (2D/3D plans) to pass to vision model
        
    Returns:
        GenerateBOQResult with items and grand total
    """
    try:
        # NOTE: BOQ mock/estimation layer does not hit gemini API directly for image fallbacks.
        # But we pass back an empty warnings list to satisfy the signature correctly.
        from app.ai.estimation import service as estimation_service
        
        warnings = []
        
        # Use singleton estimation service
        
        # Build requirements text from context and prompt
        summaries = context.get("summaries", [])
        
        if prompt:
            requirements_text = f"Project requirements: {prompt}\n\n"
        else:
            requirements_text = "Generate BOQ for the following floor plan:\n\n"
        
        for s in summaries:
            if isinstance(s, dict):
                requirements_text += f"- {s.get('title', 'Room')}: {s.get('text', '')}\n"
            else:
                requirements_text += f"- {getattr(s, 'title', 'Room')}: {getattr(s, 'text', '')}\n"
        
        # Calculate BOQ
        result = estimation_service.calculate_boq(requirements_text, image_urls=image_urls)
        
        if "error" in result:
            return GenerateBOQResult(
                success=False,
                warnings=warnings,
                error=result.get("error")
            )
        
        # Convert to BOQItem objects
        items = []
        for m in result.get("materials", []):
            items.append(BOQItem(
                work_item=m.get("name", "Unknown"),
                material=m.get("name"),
                unit=m.get("unit", "pcs"),
                quantity=float(m.get("estimated_quantity", 0) or 0),
                rate=float(m.get("unit_price", 0) or 0),
                total=float(m.get("total_price", 0) or 0),
                currency=result.get("currency", "AED")
            ))
        
        return GenerateBOQResult(
            items=items,
            grand_total=result.get("total_estimated_cost", 0.0),
            warnings=warnings,
            success=True
        )
        
    except Exception as e:
        return GenerateBOQResult(
            success=False,
            error=_map_error(str(e))
        )


def generate_room(room_type: str, prompt: str, project_id: str = "default", style: str = "sketchup") -> RoomRenderResult:
    """
    Generate a 3D render for a specific room.
    
    Args:
        room_type: Type of the room (e.g., "Bedroom")
        prompt: Description/instructions for rendering
        project_id: Project ID
        style: Rendering style
        
    Returns:
        RoomRenderResult with image URL
    """
    try:
        from app.ai.rendering import service as rendering_service
        
        # Use singleton rendering service
        
        # Generate room render
        result, warnings = rendering_service.generate_room(room_type, prompt, style=style)
        
        if not result:
            return RoomRenderResult(
                success=False,
                warnings=warnings,
                error="Room rendering failed"
            )
            
        return RoomRenderResult(
            image_bytes=result,
            warnings=warnings,
            success=True
        )
        
    except Exception as e:
        return RoomRenderResult(
            success=False,
            error=_map_error(str(e))
        )


# ============================================================================
# Health Check
# ============================================================================


def check_ai_health() -> dict:
    """Check if AI services are properly configured."""
    return ai_settings.validate()
