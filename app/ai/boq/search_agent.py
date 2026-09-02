from strands import tool
from dotenv import load_dotenv
import asyncio
import os
from langchain_openai import OpenAIEmbeddings
from pinecone import Pinecone
from typing import AsyncIterator, Awaitable, Callable, List, Dict, Any, Optional
from app.ai.config import ai_settings
from app.ai.llm_call_logging import log_llm_call
from pydantic import BaseModel, Field
import json
import time
from openai import AsyncOpenAI, OpenAI

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")


def _openrouter_headers() -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if ai_settings.OPENROUTER_SITE_URL:
        headers["HTTP-Referer"] = ai_settings.OPENROUTER_SITE_URL
    if ai_settings.OPENROUTER_APP_NAME:
        headers["X-Title"] = ai_settings.OPENROUTER_APP_NAME
    return headers


client = OpenAI(
    api_key=ai_settings.OPENROUTER_API_KEY,
    base_url=ai_settings.OPENROUTER_BASE_URL,
    default_headers=_openrouter_headers(),
)

async_client = AsyncOpenAI(
    api_key=ai_settings.OPENROUTER_API_KEY,
    base_url=ai_settings.OPENROUTER_BASE_URL,
    default_headers=_openrouter_headers(),
)

_search_semaphore = asyncio.Semaphore(8)
PricingProgressCallback = Callable[[Dict[str, Any]], Awaitable[None]]

print(PINECONE_INDEX_NAME)
pinecone_client_instance = None
pinecone_index_instance = None
embedding_model = None


# ==================== PYDANTIC MODELS ====================

class MaterialInfo(BaseModel):
    """Information about a single material from internal database"""
    material_name: str = Field(description="Name of the material")
    category: str = Field(description="Category of the material (e.g., Wood, Cement, Steel)")
    description: str = Field(description="Detailed description of the material")
    rate: str = Field(description="Price/rate of the material as a string")
    unit: Optional[str] = Field(default="", description="Unit of measurement (e.g., per sqm, per bag)")
    remarks: Optional[str] = Field(default="", description="Additional remarks or notes")


class VectorSearchResult(BaseModel):
    """Structured output from vector search agent"""
    materials: List[MaterialInfo] = Field(description="List of materials found in internal knowledge base")
    total_materials_found: int = Field(description="Total number of materials found")
    search_query: str = Field(description="The original search query")
    data_source: str = Field(default="Internal Pinecone Knowledge Base", description="Source of the data")


class WebPriceInfo(BaseModel):
    """Information about material pricing from web sources"""
    material_name: str = Field(description="Name of the material")
    price: str = Field(description="Price found on the web")
    source: str = Field(description="Website or platform name")
    url: Optional[str] = Field(default="", description="URL of the source")
    currency: Optional[str] = Field(default="AED", description="Currency of the price")
    location: Optional[str] = Field(default="Dubai", description="Location/market for the price")


class WebSearchResult(BaseModel):
    """Structured output from web search agent"""
    materials: List[WebPriceInfo] = Field(description="List of material prices found on the web")
    total_results_found: int = Field(description="Total number of web results found")
    search_query: str = Field(description="The original search query")
    data_source: str = Field(default="Web Search", description="Source of the data")


class CombinedMaterialPricing(BaseModel):
    """Combined pricing information from both internal and external sources"""
    material_name: str = Field(description="Name of the material")
    internal_data: Optional[MaterialInfo] = Field(default=None, description="Data from internal knowledge base")
    web_data: Optional[List[WebPriceInfo]] = Field(default=None, description="Data from web sources")
    has_internal_data: bool = Field(description="Whether internal data was found")
    has_web_data: bool = Field(description="Whether web data was found")


class CombinedSearchResult(BaseModel):
    """Structured output combining both internal and web search results"""
    materials: List[CombinedMaterialPricing] = Field(description="List of materials with combined pricing data")
    total_materials_searched: int = Field(description="Total number of materials searched")
    search_query: str = Field(description="The original search query")
    summary: str = Field(description="Brief summary of findings")


# --- Pinecone Functions ---

def initialize_pinecone_client():
    global pinecone_client_instance, pinecone_index_instance, embedding_model
    if pinecone_index_instance is None:
        if not PINECONE_API_KEY or not PINECONE_INDEX_NAME:
            print("Pinecone environment variables not set.")
            return None

        try:
            pinecone_client_instance = Pinecone(api_key=PINECONE_API_KEY)
            pinecone_index_instance = pinecone_client_instance.Index(PINECONE_INDEX_NAME)

            if embedding_model is None:
                print("Initializing OpenAI Embeddings model...")
                embedding_model = OpenAIEmbeddings(openai_api_key=OPENAI_API_KEY)

            pinecone_index_instance.describe_index_stats()
            print(f"Successfully connected to Pinecone index: {PINECONE_INDEX_NAME}")
        except Exception as e:
            print(f"Error initializing Pinecone: {e}")
            pinecone_index_instance = None
    return pinecone_index_instance


def vector_search(query_text: str, top_k: int = 3) -> List[Dict[str, Any]]:
    global pinecone_index_instance, embedding_model
    initialize_pinecone_client()
    if pinecone_index_instance is None:
        print("Pinecone not initialized. Skipping vector search.")
        return []

    try:
        with log_llm_call(
            provider="OpenAI",
            model=getattr(embedding_model, "model", "OpenAIEmbeddings default"),
            api="https://api.openai.com/v1/embeddings",
            operation="material vector-search embedding",
        ):
            query_embedding = embedding_model.embed_query(query_text)
        result = pinecone_index_instance.query(
            vector=query_embedding,
            top_k=top_k,
            include_values=False,
            include_metadata=True
        )
        return result.get("matches", [])
    except Exception as e:
        print(f"Pinecone vector search error: {e}")
    return []


# ==================== ASYNC DIRECT SEARCH FUNCTIONS ====================

def _empty_vector_result(query: str) -> VectorSearchResult:
    return VectorSearchResult(
        materials=[],
        total_materials_found=0,
        search_query=query,
        data_source="Internal Pinecone Knowledge Base",
    )


def _empty_web_result(query: str, region: str) -> WebSearchResult:
    query_short = query[:500] if len(query) > 500 else query
    return WebSearchResult(
        materials=[],
        total_results_found=0,
        search_query=f"{query_short} in {region}",
        data_source="Web Search",
    )


def _strip_json_markdown(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned
    return cleaned.strip()


def _vector_matches_to_result(query: str, matches: List[Dict[str, Any]]) -> VectorSearchResult:
    materials: List[MaterialInfo] = []
    for match in matches:
        metadata = match.get("metadata", {})
        material = MaterialInfo(
            material_name=metadata.get("text", "Unknown"),
            category=metadata.get("Category", ""),
            description=metadata.get("Description", ""),
            rate=metadata.get("Rate", "Rate not available"),
            unit="",
            remarks="",
        )
        materials.append(material)

    return VectorSearchResult(
        materials=materials,
        total_materials_found=len(materials),
        search_query=query,
        data_source="Internal Pinecone Knowledge Base",
    )


def _search_internal_knowledge_sync(query: str) -> VectorSearchResult:
    print(f"[internal] Searching internal knowledge base for: '{query}'")
    start_time = time.time()

    try:
        matches = vector_search(query_text=query)
        result = _vector_matches_to_result(query, matches) if matches else _empty_vector_result(query)
        suffix = f"{result.total_materials_found} results" if result.materials else "no results"
        print(f"[internal] Completed in {time.time() - start_time:.2f}s ({suffix})")
        return result
    except Exception as e:
        print(f"[internal] Error in search_internal_knowledge: {e}")
        return _empty_vector_result(query)


@tool
def search_internal_knowledge(query: str) -> str:
    """
    Search the internal Pinecone knowledge base for material pricing and information.

    Args:
        query (str): The material name(s) to search for.

    Returns:
        str: JSON string with VectorSearchResult structure.
    """
    return _search_internal_knowledge_sync(query).model_dump_json(indent=2)


async def _async_internal_search(query: str) -> VectorSearchResult:
    return await asyncio.to_thread(_search_internal_knowledge_sync, query)


def _build_web_parsing_prompt(query: str, region: str, web_results_text: str) -> str:
    query_short = query[:500] if len(query) > 500 else query
    text_short = web_results_text[:1500] if len(web_results_text) > 1500 else web_results_text

    return f"""Parse web search results into JSON.

Query: {query_short}
Region: {region}

Web Results:
{text_short}

Return ONLY this JSON format:
{{
  "materials": [
    {{"material_name": "name", "price": "price info", "source": "source name", "url": "", "currency": "AED", "location": "{region}"}}
  ],
  "total_results_found": 1,
  "search_query": "{query_short} in {region}",
  "data_source": "Web Search"
}}

If no pricing found, return empty materials array. Use "{region}" as the location field. JSON only:"""


def _parse_web_result_json(parsed_text: str) -> WebSearchResult:
    parsed_json = json.loads(_strip_json_markdown(parsed_text))
    return WebSearchResult(**parsed_json)


def _search_web_sources_sync(query: str, region: str = "Dubai") -> WebSearchResult:
    print(f"[web] Searching web for: '{query}' in region: '{region}'")
    start_time = time.time()

    try:
        search_input = f"Find current market prices for {query} in {region}. Include specific prices, supplier names, and sources."

        with log_llm_call(
            provider="OpenRouter",
            model=ai_settings.OPENROUTER_MODEL_BOQ,
            api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/responses",
            operation="material-price web search",
        ):
            response = client.responses.create(
                model=ai_settings.OPENROUTER_MODEL_BOQ,
                tools=[{
                    "type": "openrouter:web_search",
                    "parameters": {
                        "max_results": 3,
                        "max_total_results": 6,
                        "search_context_size": "medium",
                    }
                }],
                input=search_input,
            )

        web_results_text = response.output_text if response.output_text else ""

        print(f"[web] Raw response length: {len(web_results_text)} chars")
        if web_results_text:
            print(f"[web] Preview: {web_results_text[:200]}...")

        if not web_results_text or len(web_results_text) < 20:
            print(f"[web] Completed in {time.time() - start_time:.2f}s (no results)")
            return _empty_web_result(query, region)

        with log_llm_call(
            provider="OpenRouter",
            model=ai_settings.OPENROUTER_MODEL_BOQ,
            api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            operation="web-search result parsing",
        ):
            parse_response = client.chat.completions.create(
                model=ai_settings.OPENROUTER_MODEL_BOQ,
                messages=[{"role": "user", "content": _build_web_parsing_prompt(query, region, web_results_text)}],
                temperature=0,
                max_tokens=1000,
                timeout=10,
            )

        parsed_text = parse_response.choices[0].message.content or ""
        validated = _parse_web_result_json(parsed_text)
        print(f"[web] Completed in {time.time() - start_time:.2f}s ({validated.total_results_found} results)")
        return validated
    except Exception as e:
        print(f"[web] Error in search_web_sources: {e}")
        return _empty_web_result(query, region)


@tool
def search_web_sources(query: str, region: str = "Dubai") -> str:
    """
    Search web sources for current market prices and material information.

    Args:
        query (str): The material name(s) to search for.
        region (str): The region/location for pricing (default: "Dubai")

    Returns:
        str: JSON string with WebSearchResult structure.
    """
    return _search_web_sources_sync(query, region).model_dump_json(indent=2)


async def _async_web_search(query: str, region: str) -> WebSearchResult:
    print(f"[web] Searching web for: '{query}' in region: '{region}'")
    start_time = time.time()

    try:
        search_input = f"Find current market prices for {query} in {region}. Include specific prices, supplier names, and sources."

        with log_llm_call(
            provider="OpenRouter",
            model=ai_settings.OPENROUTER_MODEL_BOQ,
            api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/responses",
            operation="async material-price web search",
        ):
            response = await async_client.responses.create(
                model=ai_settings.OPENROUTER_MODEL_BOQ,
                tools=[{
                    "type": "openrouter:web_search",
                    "parameters": {
                        "max_results": 3,
                        "max_total_results": 6,
                        "search_context_size": "medium",
                    }
                }],
                input=search_input,
            )

        web_results_text = response.output_text if response.output_text else ""

        print(f"[web] Raw response length: {len(web_results_text)} chars")
        if web_results_text:
            print(f"[web] Preview: {web_results_text[:200]}...")

        if not web_results_text or len(web_results_text) < 20:
            print(f"[web] Completed in {time.time() - start_time:.2f}s (no results)")
            return _empty_web_result(query, region)

        with log_llm_call(
            provider="OpenRouter",
            model=ai_settings.OPENROUTER_MODEL_BOQ,
            api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            operation="async web-search result parsing",
        ):
            parse_response = await async_client.chat.completions.create(
                model=ai_settings.OPENROUTER_MODEL_BOQ,
                messages=[{"role": "user", "content": _build_web_parsing_prompt(query, region, web_results_text)}],
                temperature=0,
                max_tokens=1000,
                timeout=10,
            )

        parsed_text = parse_response.choices[0].message.content or ""
        validated = _parse_web_result_json(parsed_text)
        print(f"[web] Completed in {time.time() - start_time:.2f}s ({validated.total_results_found} results)")
        return validated
    except Exception as e:
        print(f"[web] Error in _async_web_search: {e}")
        return _empty_web_result(query, region)


async def _parse_materials_list(query: str) -> List[str]:
    prompt = f"""Extract the material names to price from the user's query.

Rules:
- Return ONLY a JSON array of strings.
- Normalize names but keep important specifications such as size, thickness, grade, and unit.
- Create separate entries for variants. For example, "plywood 18mm and 25mm" becomes ["plywood 18mm", "plywood 25mm"].
- If the query contains one material, return an array with one item.
- Do not include region/location words unless they are part of the material itself.

Query: {query}

JSON array only:"""

    try:
        with log_llm_call(
            provider="OpenRouter",
            model=ai_settings.OPENROUTER_MODEL_BOQ,
            api=f"{ai_settings.OPENROUTER_BASE_URL.rstrip('/')}/chat/completions",
            operation="material-list parsing",
        ):
            response = await async_client.chat.completions.create(
                model=ai_settings.OPENROUTER_MODEL_BOQ,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                timeout=10,
            )
        parsed_text = response.choices[0].message.content or ""
        parsed_json = json.loads(_strip_json_markdown(parsed_text))
        if not isinstance(parsed_json, list):
            raise ValueError("Material parser did not return a list")

        materials: List[str] = []
        seen = set()
        for item in parsed_json:
            material = str(item).strip()
            if not material:
                continue
            key = material.lower()
            if key not in seen:
                materials.append(material)
                seen.add(key)

        return materials or [query]
    except Exception as e:
        print(f"[pricing] Error parsing materials list: {e}")
        return [query]


async def _emit_pricing_progress(
    progress_callback: Optional[PricingProgressCallback],
    **payload: Any,
) -> None:
    if progress_callback:
        await progress_callback({"type": "pricing_progress", **payload})


def _web_source_hints(web_result: WebSearchResult) -> List[Dict[str, str]]:
    sources: List[Dict[str, str]] = []
    seen = set()
    for item in web_result.materials:
        label = (item.source or item.url or "Web result").strip()
        key = (label.lower(), (item.url or "").lower())
        if key in seen:
            continue
        seen.add(key)
        source = {
            "kind": "web",
            "label": label,
        }
        if item.url:
            source["url"] = item.url
        sources.append(source)
        if len(sources) >= 4:
            break
    return sources


async def _search_one_material(
    material: str,
    region: str,
    progress_callback: Optional[PricingProgressCallback] = None,
) -> CombinedMaterialPricing:
    try:
        await _emit_pricing_progress(
            progress_callback,
            stage="material_start",
            material=material,
            status=f"Researching prices for {material}...",
            sources=[
                {"kind": "internal", "label": "Internal Pinecone Knowledge Base"},
                {"kind": "web", "label": f"OpenRouter web search in {region}"},
            ],
        )

        async with _search_semaphore:
            async def run_internal_search() -> VectorSearchResult:
                await _emit_pricing_progress(
                    progress_callback,
                    stage="source_start",
                    material=material,
                    status=f"Checking internal catalog for {material}...",
                    sources=[{"kind": "internal", "label": "Internal Pinecone Knowledge Base"}],
                )
                result = await _async_internal_search(material)
                await _emit_pricing_progress(
                    progress_callback,
                    stage="source_complete",
                    material=material,
                    status=f"Internal catalog checked for {material}.",
                    sources=[{"kind": "internal", "label": "Internal Pinecone Knowledge Base"}],
                    result_count=result.total_materials_found,
                )
                return result

            async def run_web_search() -> WebSearchResult:
                await _emit_pricing_progress(
                    progress_callback,
                    stage="source_start",
                    material=material,
                    status=f"Searching web pricing sources for {material}...",
                    sources=[{"kind": "web", "label": f"OpenRouter web search in {region}"}],
                )
                result = await _async_web_search(material, region)
                source_hints = _web_source_hints(result)
                await _emit_pricing_progress(
                    progress_callback,
                    stage="source_complete",
                    material=material,
                    status=f"Web pricing search checked for {material}.",
                    sources=source_hints or [{"kind": "web", "label": f"OpenRouter web search in {region}"}],
                    result_count=result.total_results_found,
                )
                return result

            internal_result, web_result = await asyncio.gather(
                run_internal_search(),
                run_web_search(),
                return_exceptions=True,
            )

        if isinstance(internal_result, Exception):
            print(f"[pricing] Internal search failed for '{material}': {internal_result}")
            internal_result = _empty_vector_result(material)
        if isinstance(web_result, Exception):
            print(f"[pricing] Web search failed for '{material}': {web_result}")
            web_result = _empty_web_result(material, region)

        internal_data = internal_result.materials[0] if internal_result.materials else None
        web_data = web_result.materials or None
        await _emit_pricing_progress(
            progress_callback,
            stage="material_complete",
            material=material,
            status=f"Finished pricing research for {material}.",
            sources=_web_source_hints(web_result),
            has_internal_data=internal_data is not None,
            has_web_data=bool(web_data),
        )

        return CombinedMaterialPricing(
            material_name=material,
            internal_data=internal_data,
            web_data=web_data,
            has_internal_data=internal_data is not None,
            has_web_data=bool(web_data),
        )
    except Exception as e:
        print(f"[pricing] Error searching material '{material}': {e}")
        return CombinedMaterialPricing(
            material_name=material,
            internal_data=None,
            web_data=None,
            has_internal_data=False,
            has_web_data=False,
        )


def _build_summary(materials: List[CombinedMaterialPricing], region: str) -> str:
    if not materials:
        return f"No materials found in {region} region"

    internal_count = sum(1 for material in materials if material.has_internal_data)
    web_count = sum(1 for material in materials if material.has_web_data)
    no_data_count = len(materials) - sum(
        1 for material in materials if material.has_internal_data or material.has_web_data
    )

    parts = [
        f"Found pricing data for {len(materials)} materials in {region} region",
        f"{internal_count} with internal data",
        f"{web_count} with web data",
    ]
    if no_data_count:
        parts.append(f"{no_data_count} with no data")
    return "; ".join(parts)


@tool
async def search_material_pricing(query: str, region: str = "Dubai") -> AsyncIterator[Any]:
    """
    Search for material pricing with deterministic async parallel execution.

    Args:
        query (str): Material name or pricing query.
        region (str): Region/location for web pricing search (default: "Dubai").

    Returns:
        str: Structured JSON with CombinedSearchResult format.
    """
    print(f"\n{'='*80}")
    print(f"[pricing] Starting material pricing search for: '{query}' in region: '{region}'")
    print(f"{'='*80}")
    overall_start = time.time()

    try:
        progress_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        async def queue_progress(payload: Dict[str, Any]) -> None:
            await progress_queue.put(payload)

        yield {
            "type": "pricing_progress",
            "stage": "parse_start",
            "status": "Identifying materials to price...",
            "sources": [],
        }

        materials = await _parse_materials_list(query)
        print(f"[pricing] Parsed {len(materials)} materials: {materials}")

        yield {
            "type": "pricing_progress",
            "stage": "parse_complete",
            "status": f"Pricing research queued for {len(materials)} materials.",
            "materials": materials,
            "sources": [
                {"kind": "internal", "label": "Internal Pinecone Knowledge Base"},
                {"kind": "web", "label": f"OpenRouter web search in {region}"},
            ],
        }

        search_task = asyncio.gather(
            *(_search_one_material(material, region, queue_progress) for material in materials),
            return_exceptions=False,
        )

        while not search_task.done():
            try:
                progress = await asyncio.wait_for(progress_queue.get(), timeout=0.25)
                yield progress
            except asyncio.TimeoutError:
                continue

        per_material = await search_task
        while not progress_queue.empty():
            yield progress_queue.get_nowait()

        result = CombinedSearchResult(
            materials=per_material,
            total_materials_searched=len(per_material),
            search_query=f"{query} (region: {region})",
            summary=_build_summary(per_material, region),
        )

        print(f"[pricing] Search completed successfully: {result.total_materials_searched} materials searched")
        print(f"[pricing] Total time: {time.time() - overall_start:.2f}s")

        yield {
            "type": "pricing_progress",
            "stage": "pricing_complete",
            "status": "Pricing research complete. Preparing summary...",
            "sources": [],
        }
        yield result.model_dump_json(indent=2)
    except Exception as e:
        print(f"[pricing] Error in search_material_pricing: {e}")
        yield CombinedSearchResult(
            materials=[],
            total_materials_searched=0,
            search_query=f"{query} (region: {region})",
            summary="Error occurred",
        ).model_dump_json(indent=2)
