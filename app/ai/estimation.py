"""
Estimation Service - BOQ and Cost Estimation.

Uses OpenAI to:
- Extract construction materials from requirements
- Estimate material quantities
- Search for market pricing
- Generate Bill of Quantities (BOQ)
"""

import json
import re
from typing import Optional, List

from openai import OpenAI
from pydantic import BaseModel, Field, field_validator

from app.ai.config import ai_settings
from app.ai.llm_call_logging import log_llm_call


# Initialize OpenAI client
openai_client = OpenAI(api_key=ai_settings.OPENAI_API_KEY)


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class MaterialItem(BaseModel):
    """Single material with quantity and unit."""
    name: str = Field(description="Material name (standardized)")
    estimated_quantity: str = Field(description="Estimated quantity as string")
    unit: str = Field(description="Unit of measurement (m³, m², kg, pcs, L, m)")
    
    @field_validator('estimated_quantity', mode='before')
    @classmethod
    def convert_quantity_to_string(cls, v):
        """Convert numeric quantities to strings."""
        if isinstance(v, (int, float)):
            return str(v)
        return v


class MaterialsExtraction(BaseModel):
    """Materials list extracted from requirements."""
    materials: List[MaterialItem] = Field(description="List of construction materials")
    total_count: int = Field(description="Total number of materials")


# ============================================================================
# MATERIALS EXTRACTION
# ============================================================================

def extract_materials_direct(requirements_text: str, image_urls: Optional[list[str]] = None) -> MaterialsExtraction:
    """
    Direct OpenAI API call for materials extraction.
    """
    if not requirements_text or len(requirements_text.strip()) < 10:
        return MaterialsExtraction(materials=[], total_count=0)
    
    # Truncate long text to reduce tokens
    text_sample = requirements_text[:2000]
    
    prompt = f"""Extract construction materials with quantities from the requirements. Return ONLY valid JSON:

{{
  "materials": [
    {{"name": "material_name", "estimated_quantity": "number_as_string", "unit": "unit"}}
  ],
  "total_count": number
}}

CRITICAL: estimated_quantity MUST be a string (e.g., "150" not 150)

Rules:
- Standard units: m³, m², kg, L, pcs, m
- Infer quantities from context if needed
- Merge duplicates
- Return quantities as strings

Requirements:
{text_sample}

JSON only:"""

    if image_urls:
        content = [
            {"type": "text", "text": prompt}
        ]
        for url in image_urls:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": url,
                    "detail": "high"
                }
            })
    else:
        content = prompt

    try:
        with log_llm_call(
            provider="OpenAI",
            model="gpt-4o-mini",
            api="https://api.openai.com/v1/chat/completions",
            operation="materials extraction",
        ):
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": content}],
                temperature=0,
                max_tokens=1000
            )
        
        raw = response.choices[0].message.content.strip()
        
        # Clean markdown if present
        if raw.startswith('```'):
            lines = raw.split('\n')
            raw = '\n'.join(lines[1:-1]) if len(lines) > 2 else raw
        
        parsed = json.loads(raw)
        
        # Convert numeric quantities to strings before validation
        if 'materials' in parsed:
            for item in parsed['materials']:
                if 'estimated_quantity' in item and isinstance(item['estimated_quantity'], (int, float)):
                    item['estimated_quantity'] = str(item['estimated_quantity'])
        
        validated = MaterialsExtraction(**parsed)
        return validated
        
    except Exception as e:
        print(f"Materials extraction error: {e}")
        return MaterialsExtraction(materials=[], total_count=0)


# ============================================================================
# PRICING SEARCH
# ============================================================================

def search_material_pricing_simple(material_name: str) -> dict:
    """
    Pricing search using OpenAI with market knowledge.
    """
    try:
        prompt = f"""Provide realistic market pricing estimates for: {material_name}

Return JSON format:
{{
  "material_name": "{material_name}",
  "estimated_price": "number",
  "unit": "unit",
  "currency": "AED",
  "source": "Market estimate",
  "confidence": "medium"
}}

Provide best estimate based on Dubai/UAE construction market. JSON only:"""
        
        with log_llm_call(
            provider="OpenAI",
            model="gpt-4o-mini",
            api="https://api.openai.com/v1/chat/completions",
            operation="material pricing estimate",
        ):
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=300
            )
        
        raw = response.choices[0].message.content.strip()
        if raw.startswith('```'):
            lines = raw.split('\n')
            raw = '\n'.join(lines[1:-1]) if len(lines) > 2 else raw
        
        return json.loads(raw)
        
    except Exception as e:
        print(f"Pricing search error: {e}")
        return {
            "material_name": material_name,
            "estimated_price": "100",
            "unit": "unit",
            "currency": "AED",
            "source": "Default estimate"
        }


# ============================================================================
# ESTIMATION SERVICE
# ============================================================================

class EstimationService:
    """BOQ estimation service for construction projects."""
    
    def extract_materials(self, requirements_text: str, image_urls: Optional[list[str]] = None) -> dict:
        """Extract materials list from requirements."""
        result = extract_materials_direct(requirements_text, image_urls=image_urls)
        return result.model_dump()

    def get_material_pricing(self, material_name: str) -> str:
        """Get pricing for a specific material."""
        result = search_material_pricing_simple(material_name)
        return json.dumps(result, indent=2)

    def calculate_boq(self, requirements_text: str, image_urls: Optional[list[str]] = None) -> dict:
        """
        Full BOQ calculation with pricing.
        
        Args:
            requirements_text: Full project requirements
            image_urls: Optional image URLs to assist with estimation
            
        Returns:
            dict: BOQ with materials, pricing, and total cost
        """
        # 1. Extract Materials
        extraction = self.extract_materials(requirements_text, image_urls=image_urls)
        
        if "error" in extraction:
            return extraction
            
        materials = extraction.get("materials", [])
        total_estimated_cost = 0
        
        # 2. Enrich with Pricing
        for m in materials:
            name = m.get('name')
            qty_str = m.get('estimated_quantity', '0')
            
            # Parse quantity
            try:
                num_match = re.search(r"[-+]?\d*\.?\d+|\d+", str(qty_str))
                qty = float(num_match.group()) if num_match else 0.0
            except:
                qty = 0.0
            
            # Get pricing
            price_info_json = self.get_material_pricing(name)
            
            unit_price = 100.0  # Default fallback
            pricing_source = "Default estimate"
            
            try:
                price_data = json.loads(price_info_json)
                price_str = str(price_data.get('estimated_price', '100'))
                price_match = re.search(r"(\d+(?:,\d{3})*(?:\.\d{2})?)", price_str)
                if price_match:
                    unit_price = float(price_match.group(1).replace(',', ''))
                    pricing_source = price_data.get('source', 'Market estimate')
            except Exception as e:
                print(f"Error parsing pricing for {name}: {e}")

            m['unit_price'] = unit_price
            m['total_price'] = qty * unit_price
            m['pricing_source'] = pricing_source
            m['estimated_quantity'] = qty
            
            total_estimated_cost += m['total_price']
            
        return {
            "materials": materials,
            "total_estimated_cost": total_estimated_cost,
            "currency": "AED"
        }


# Singleton instance
service = EstimationService()
