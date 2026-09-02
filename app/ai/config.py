import os
import tempfile
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from backend directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)


class AISettings:
    """AI-related settings loaded from environment."""
    
    PROJECT_NAME = "Floor-Plan AI Backend"
    VERSION = "1.0.0"
    
    # API Keys - loaded from environment
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
    SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")

    # Google Cloud Config (for Vertex AI)
    GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
    GOOGLE_CLOUD_LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION")
    
    # Pinecone Configuration
    PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "construction-materials")
    
    # Paths - use the backend's directory for outputs
    MEDIA_ROOT = os.getenv("MEDIA_ROOT", str(BASE_DIR / "media"))
    # Scratch space for the few things that genuinely need a path on disk:
    # the multi-turn edit history (a few KB of JSON) and documents staged for
    # the BOQ agent's file tools. Generated images are NEVER written here —
    # they are returned as bytes and stored by Django under media/, which is
    # the product's only image store.
    SCRATCH_DIR = os.getenv(
        "AI_SCRATCH_DIR", os.path.join(tempfile.gettempdir(), "kraios-ai-scratch")
    )
    
    # AI Models
    # GEMINI_MODEL_ANALYSIS = "gemini-2.5-flash-image"
    # GEMINI_MODEL_GENERATION = "gemini-3-pro-image-preview"
    # GEMINI_MODEL_IMAGE_GEN = "gemini-2.5-flash-image"
    # GEMINI_MODEL_2D_DESIGN = "gemini-2.5-flash-image"\

    GEMINI_MODEL_ANALYSIS   = "gemini-3-pro-image-preview"
    # GEMINI_MODEL_ANALYSIS   = "gemini-3.1-flash-image-preview"
    GEMINI_MODEL_GENERATION = "gemini-3-pro-image-preview"
    GEMINI_MODEL_2D_DESIGN  = "gemini-3-pro-image-preview"
    GEMINI_MODEL_LOGIC = "gemini-3-pro-image-preview"  # For chat interpretation
    # GEMINI_MODEL_IMAGE = "gemini-3-pro-image-preview"  # For image generation/editing
    GEMINI_MODEL_IMAGE = "gemini-3.1-flash-image-preview"  # For image generation/editing
    GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta")
    GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3-pro-image-preview")
    GEMINI_REQUEST_TIMEOUT = float(os.getenv("GEMINI_REQUEST_TIMEOUT", "120"))
    MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
    MAX_IMAGE_DIM = int(os.getenv("MAX_IMAGE_DIM", "1600"))
    PDF_RENDER_DPI = int(os.getenv("PDF_RENDER_DPI", "180"))
    SKETCHUP_STYLE_REF = Path(
        os.getenv(
            "SKETCHUP_STYLE_REF",
            str(Path(__file__).resolve().parent / "assets" / "sketchup_style.png"),
        )
    )

    OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    OPENROUTER_SITE_URL = os.getenv("OPENROUTER_SITE_URL")
    OPENROUTER_APP_NAME = os.getenv("OPENROUTER_APP_NAME", "Floor-Plan AI Backend")
    OPENROUTER_MODEL_ANALYSIS = 'google/gemini-3-pro-image-preview'
    OPENROUTER_MODEL_RENDER = 'google/gemini-3-pro-image-preview'
    # OPENROUTER_MODEL_FLOORPLAN = os.getenv("OPENROUTER_MODEL_FLOORPLAN", os.getenv("BOQ_MODEL_ID", "anthropic/claude-opus-4.7"))
    # OPENROUTER_MODEL_FLOORPLAN = os.getenv("OPENROUTER_MODEL_FLOORPLAN", os.getenv("BOQ_MODEL_ID", "anthropic/claude-fable-5"))
    OPENROUTER_MODEL_FLOORPLAN = os.getenv("OPENROUTER_MODEL_FLOORPLAN", os.getenv("BOQ_MODEL_ID", "openai/gpt-5.6-sol"))
    # Output budget for floorplan JSON extraction. Complex CAD sheets (40-60
    # walls + rooms + description) overflow small budgets, and a truncated JSON
    # is unparseable -> schema-retry -> the whole vision call is paid again.
    OPENROUTER_FLOORPLAN_MAX_TOKENS = int(os.getenv("OPENROUTER_FLOORPLAN_MAX_TOKENS", "16000"))
    # OPENROUTER_MODEL_ANALYSIS = 'google/gemini-3.1-flash-image-preview'
    # OPENROUTER_MODEL_RENDER = 'google/gemini-3.1-flash-image-preview'

    # OPENROUTER_MODEL_BOQ = os.getenv("OPENROUTER_MODEL_BOQ", "anthropic/claude-opus-4.7")
    OPENROUTER_MODEL_BOQ = os.getenv("OPENROUTER_MODEL_BOQ", "anthropic/claude-fable-5")
    # OpenRouter model used by the guided (sketchout) render pipeline
    # -> https://openrouter.ai/google/gemini-3-pro-image-preview
    OPENROUTER_GUIDED_RENDER_MODEL = os.getenv(
        "OPENROUTER_GUIDED_RENDER_MODEL", "google/gemini-3-pro-image-preview"
    )
    # OpenRouter model used ONLY by the isometric / bird's-eye camera re-render
    # (RenderingService.generate_isometric_from_image). Every other image call
    # stays on OPENROUTER_MODEL_RENDER. If this model fails, the call falls back
    # to OPENROUTER_MODEL_RENDER and then to Vertex Gemini, exactly as before.
    # NOTE: must be an OpenRouter model whose output_modalities include "image".
    # openai/gpt-5.6-sol is text-output only and will NOT work here; the OpenAI
    # image-capable slugs are gpt-5.4-image-2, gpt-5-image, gpt-5-image-mini.
    OPENROUTER_MODEL_ISOMETRIC = os.getenv(
        "OPENROUTER_MODEL_ISOMETRIC", "openai/gpt-5.4-image-2"
    )
    # --- Fidelity / verification guardrails ------------------------------
    # Additive QA gates (NOT design prompts). Every gate fails OPEN: if the
    # verifier errors, generation still proceeds. All knobs env-overridable.
    #
    # Gate 1 (JSON fidelity, pre-snapshot): audits the extracted FloorPlan
    # against the 2D plan and self-corrects before the browser builds the model.
    FIDELITY_GATE_ENABLED = os.getenv("FIDELITY_GATE_ENABLED", "true").lower() == "true"
    FIDELITY_MIN_SCORE = int(os.getenv("FIDELITY_MIN_SCORE", "75"))
    FIDELITY_MAX_EXTRACTION_ATTEMPTS = int(os.getenv("FIDELITY_MAX_EXTRACTION_ATTEMPTS", "2"))
    # Lean render check (user-approved replacement for the removed best-of-N
    # Gate 2): ONE post-render verify with a FAST judge, and at most ONE
    # corrective retry on mismatch. Do NOT grow this back into a retry loop.
    RENDER_VERIFY_ENABLED = os.getenv("RENDER_VERIFY_ENABLED", "true").lower() == "true"
    RENDER_VERIFY_MODEL = os.getenv("RENDER_VERIFY_MODEL", "anthropic/claude-haiku-4.5")
    RENDER_VERIFY_MIN_SCORE = int(os.getenv("RENDER_VERIFY_MIN_SCORE", "75"))

    VERTEX_CONFIGURED = bool(GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION)

    GROQ_API_KEY = os.getenv("GROQ_API_KEY")



    # Conversational Rendering Models
    
    OPENAI_MODEL_ID = "gpt-4o"
    
    @classmethod
    def validate(cls) -> dict:
        """Validate that required API keys are present."""
        issues = []
        if not cls.OPENROUTER_API_KEY and not cls.VERTEX_CONFIGURED and not cls.GEMINI_API_KEY:
            issues.append("No rendering provider is configured (set OPENROUTER_API_KEY or Vertex project/location)")
        if not cls.OPENAI_API_KEY:
            issues.append("OPENAI_API_KEY is missing")
        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "gemini_configured": bool(cls.GEMINI_API_KEY),
            "vertex_configured": bool(cls.VERTEX_CONFIGURED),
            "openrouter_configured": bool(cls.OPENROUTER_API_KEY),
            "openai_configured": bool(cls.OPENAI_API_KEY),
            "pinecone_configured": bool(cls.PINECONE_API_KEY),
            "serpapi_configured": bool(cls.SERPAPI_API_KEY)
        }


ai_settings = AISettings()

os.makedirs(ai_settings.SCRATCH_DIR, exist_ok=True)
