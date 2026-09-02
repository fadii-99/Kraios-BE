"""
Architect Service - 2D Floor Plan Generation.

Uses Gemini AI to generate 2D architectural floor plans from text descriptions.
"""

from google import genai
from google.genai import types
from PIL import Image
import io
import os
import base64

from app.ai.config import ai_settings
from app.ai.llm_call_logging import log_llm_call
from app.ai import prompts


def safe_print(*args, **kwargs):
    """Print helper that ignores broken pipe errors."""
    try:
        print(*args, **kwargs)
    except OSError:
        pass


class ArchitectService:
    """Service for generating 2D architectural floor plans."""
    
    def __init__(self):
        self.engines = []
        
        # 1. Primary: Google Vertex AI Client 3 pro
        try:
            print(f"Initializing Vertex AI Client: Project={ai_settings.GOOGLE_CLOUD_PROJECT}, Location={ai_settings.GOOGLE_CLOUD_LOCATION}")
            
            # The SDK will automatically pick up GOOGLE_APPLICATION_CREDENTIALS
            vertex_client = genai.Client(
                vertexai=True,
                project=ai_settings.GOOGLE_CLOUD_PROJECT,
                location=ai_settings.GOOGLE_CLOUD_LOCATION
            )
            self.engines.append({
                "client": vertex_client,
                "model_name": "gemini-3-pro-image-preview",
                "label": "Vertex 3 Pro"
            })
        except Exception as e:
            print(f"Vertex AI Client Init Warning: {e}")


        # 2. Backup: Google Gemini API Client (AI Studio) 3 pro
        try:
            gemini_3_pro_client = genai.Client(
                vertexai=False,
                api_key=ai_settings.GEMINI_API_KEY
            )
            self.engines.append({
                 "client": gemini_3_pro_client,
                 "model_name": "gemini-3-pro-image-preview",
                 "label": "Gemini 3 Pro"
            })
        except Exception as e:
            print(f"Gemini API Client (3 Pro) Init Warning: {e}")

         # 3. Backup: Google Gemini API Client (AI Studio) 2.5 Flash
        try:
            gemini_25_flash_client = genai.Client(
                vertexai=False,
                api_key=ai_settings.GEMINI_API_KEY
            )
            self.engines.append({
                 "client": gemini_25_flash_client,
                 "model_name": "gemini-2.5-flash",
                 "label": "Gemini 2.5 Flash"
            })
        except Exception as e:
            print(f"Gemini API Client (2.5 Flash) Init Warning: {e}")


    def _map_error(self, error_str: str) -> str:
        """Map technical AI errors to user-friendly messages."""
        if not error_str:
            return "An unexpected error occurred during design generation. Please try again."
            
        err_up = error_str.upper()
        
        if "429" in err_up or "RESOURCE_EXHAUSTED" in err_up or "QUOTA" in err_up:
            return "The design service is currently very busy. Please wait a minute and try again."
        
        if "404" in err_up or "NOT_FOUND" in err_up:
            return "The requested AI design model is currently offline for maintenance. Please try again in a few minutes."
            
        if "SAFETY" in err_up or "CENSOR" in err_up or "HARM" in err_up:
            return "Your request was flagged by AI safety filters. Please try phrasing your request differently."
            
        return error_str


    def generate_2d_plan(self, description: str, image_path: str = None) -> tuple[str, list]:
        """
        Generates a 2D floor plan image based on text description and optional reference image.
        
        Args:
            description: Text description of the desired floor plan
            image_path: Optional path to a reference image
            
        Returns:
            Tuple of:
            - PNG bytes of the generated image, or None if it failed entirely
            - List of warning strings if fallbacks occurred
        """
        prompt = prompts.ARCHITECT_2D_PROMPT_TEMPLATE.format(description=description)
        contents = [prompt]
        
        # If image is provided, load it and add to contents
        if image_path:
            try:
                # Determine mime type
                mime_type = "image/png"
                ext = os.path.splitext(image_path)[1].lower()
                if ext in [".jpg", ".jpeg"]:
                    mime_type = "image/jpeg"
                elif ext == ".webp":
                    mime_type = "image/webp"
                
                with open(image_path, "rb") as f:
                    image_data = f.read()
                    
                contents.append(types.Part.from_bytes(data=image_data, mime_type=mime_type))
                safe_print(f"Added reference image to generation context: {image_path}")
            except Exception as e:
                safe_print(f"Failed to load reference image: {e}")
        
        if not self.engines:
             raise Exception("No AI clients were successfully initialized.")

        warnings = []

        for i, engine in enumerate(self.engines):
            client = engine["client"]
            model_name = engine["model_name"]
            label = engine["label"]

            safe_print(f"Attempting 2D Generation with {label} ({model_name})...")
            try:
                provider = "Vertex Gemini" if label.startswith("Vertex") else "Google Gemini API"
                api = (
                    f"vertexai://{ai_settings.GOOGLE_CLOUD_PROJECT}/"
                    f"{ai_settings.GOOGLE_CLOUD_LOCATION}"
                    if provider == "Vertex Gemini"
                    else (
                        f"{ai_settings.GEMINI_BASE_URL.rstrip('/')}/models/"
                        f"{model_name}:generateContent"
                    )
                )
                with log_llm_call(
                    provider=provider,
                    model=model_name,
                    api=api,
                    operation="2D floor-plan image generation",
                ):
                    response = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            response_modalities=['TEXT', 'IMAGE'],
                        )
                    )
                
                # Parse response using the new SDK pattern
                if hasattr(response, 'parts'):
                    for part in response.parts:
                        # Check for inline data (image)
                        if hasattr(part, 'inline_data') and part.inline_data:
                            # Try using as_image() helper if available (returns PIL Image)
                            if hasattr(part, 'as_image'):
                                try:
                                    image = part.as_image()
                                    buffer = io.BytesIO()
                                    image.save(buffer, format="PNG")
                                    return buffer.getvalue(), warnings
                                except Exception as img_err:
                                    safe_print(f"Error using as_image(): {img_err}")
                            
                            # Fallback to manual byte handling
                            mime_type = part.inline_data.mime_type or "image/png"
                            ext = ".png"
                            if "jpeg" in mime_type: ext = ".jpg"
                            
                            image_data = part.inline_data.data
                            # If it's a string, it's likely base64 encoded
                            if isinstance(image_data, str):
                                image_data = base64.b64decode(image_data)
                                
                            return image_data, warnings

                safe_print(f"{label} response returned successfully but contained no inline image.")
                
            except Exception as e:
                import traceback
                error_msg = str(e)
                safe_print(f"Error generating 2D plan with {label}: {error_msg}")
                # Log traceback silently
                
                # If there is another engine, warn
                if i < len(self.engines) - 1:
                    next_label = self.engines[i+1]["label"]
                    warning = f"Current model {label} is exhausted or failed, shifting to {next_label}."
                    safe_print(warning)
                    warnings.append(warning)
                else:
                    safe_print("All models exhausted.")
                    friendly_err = self._map_error(error_msg)
                    raise Exception(friendly_err)
                    
        return None, warnings


# Singleton instance
service = ArchitectService()
