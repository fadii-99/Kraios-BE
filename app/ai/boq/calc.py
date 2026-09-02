"""Deterministic BOQ arithmetic.

The main agent must never do BOQ money math in its head — LLM mental arithmetic
is where wrong totals come from. Every number in the final table comes from here.

Computation runs in Decimal, not binary float: 0.1 + 0.2 != 0.3 in float, and a
BOQ that is off by 0.0000000001 prints as 3703.9800000000005. Values are returned
as floats so they stay JSON/tool-serializable for the agent.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
import json
import re

from strands import tool

_CENTS = Decimal("0.01")
# Strip currency symbols/codes, thousands separators and stray percent signs the
# model tends to include when it echoes a rate back ("AED 1,250.00", "5%").
_NUMERIC_NOISE = re.compile(r"[,\s%$£€]|AED|USD|GBP|EUR|SAR|QAR|INR|PKR", re.IGNORECASE)


def _dec(value: Any, field: str) -> Decimal:
    """Parse an agent-supplied number into Decimal. Blank/missing means zero."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # bool is an int subclass — reject it explicitly
        raise ValueError(f"{field}: expected a number, got a boolean")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # str() first: Decimal(0.1) would carry the binary float error in.
        return Decimal(str(value))

    cleaned = _NUMERIC_NOISE.sub("", str(value)).strip()
    if not cleaned:
        return Decimal(0)
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"{field}: {value!r} is not a valid number")


def _money(value: Decimal) -> Decimal:
    """Round to 2 decimals, half-up (commercial rounding, not banker's)."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def compute(operation: str, values: List[Any]) -> Dict[str, Any]:
    """Chain one operation over values, left to right, in Decimal."""
    op = str(operation or "").strip().lower()
    if not isinstance(values, list) or not values:
        raise ValueError("values must be a non-empty list of numbers")

    parsed = [_dec(v, f"value {i}") for i, v in enumerate(values, start=1)]

    total = parsed[0]
    for position, value in enumerate(parsed[1:], start=2):
        if op in ("add", "sum", "+"):
            total += value
        elif op in ("subtract", "minus", "-"):
            total -= value
        elif op in ("multiply", "times", "*", "x"):
            total *= value
        elif op in ("divide", "/"):
            if value == 0:
                raise ValueError(f"division by zero at value {position}")
            total /= value
        else:
            raise ValueError(
                f"unknown operation {operation!r}: use add, subtract, multiply or divide"
            )

    return {
        "operation": op,
        "values": [float(v) for v in parsed],
        "result": float(total),
        # Division rarely lands on 2dp (10/3); use this for anything shown as money.
        "result_rounded_2dp": float(_money(total)),
    }


@tool
def calculate(operation: str, values: List[float]) -> str:
    """Add, subtract, multiply or divide numbers exactly. Use this instead of doing
    arithmetic yourself whenever the numbers are money, quantities or percentages.

    Applies the operation across values left to right, so subtract of [100, 20, 5]
    is 100 - 20 - 5 = 75. Accepts messy numbers like "AED 1,250.50" or "5%".

    Args:
        operation: One of "add", "subtract", "multiply", "divide".
        values: Two or more numbers to combine (a single value is returned as-is).

    Returns:
        str: JSON with "result" (full precision) and "result_rounded_2dp"
            (use this one for any figure shown as money). Report it verbatim.
    """
    try:
        return json.dumps(compute(operation, values), indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e), "hint": "Fix the named field and call the tool again."}, indent=2)


def compute_boq(
    items: List[Dict[str, Any]],
    overhead_percent: Any = 0,
    contingency_percent: Any = 0,
    profit_margin_percent: Any = 0,
    vat_percent: Any = 0,
) -> Dict[str, Any]:
    """Compute a full BOQ. Pure function — see `calculate_boq_totals` for the tool."""
    if not isinstance(items, list):
        raise ValueError("items must be a list of BOQ line items")

    overhead_pct = _dec(overhead_percent, "overhead_percent")
    contingency_pct = _dec(contingency_percent, "contingency_percent")
    profit_pct = _dec(profit_margin_percent, "profit_margin_percent")
    vat_pct = _dec(vat_percent, "vat_percent")

    computed: List[Dict[str, Any]] = []
    unpriced: List[str] = []
    subtotal = Decimal(0)

    for index, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"item {index}: expected an object, got {type(raw).__name__}")

        description = str(raw.get("description") or raw.get("name") or f"Item {index}").strip()
        quantity = _dec(raw.get("quantity"), f"item {index} ({description}) quantity")
        rate = _dec(raw.get("rate", raw.get("unit_rate")), f"item {index} ({description}) rate")

        # Round each line before summing so the printed column actually adds up
        # to the printed subtotal.
        amount = _money(quantity * rate)
        subtotal += amount

        if rate == 0:
            unpriced.append(description)

        computed.append({
            "item_no": raw.get("item_no") or index,
            "description": description,
            "unit": str(raw.get("unit") or "").strip(),
            "quantity": float(quantity),
            "rate": float(rate),
            "amount": float(amount),
            "remarks": str(raw.get("remarks") or "").strip(),
        })

    subtotal = _money(subtotal)
    overhead = _money(subtotal * overhead_pct / 100)
    contingency = _money(subtotal * contingency_pct / 100)

    # Profit is taken on cost including overhead and contingency — the standard
    # construction build-up, and what the "Subtotal + Overhead + Contingency +
    # Profit" line in the BOQ prompt describes.
    profit_base = subtotal + overhead + contingency
    profit = _money(profit_base * profit_pct / 100)

    net_total = _money(profit_base + profit)
    vat = _money(net_total * vat_pct / 100)
    grand_total = _money(net_total + vat)

    return {
        "items": computed,
        "subtotal": float(subtotal),
        "overhead_percent": float(overhead_pct),
        "overhead": float(overhead),
        "contingency_percent": float(contingency_pct),
        "contingency": float(contingency),
        "profit_margin_percent": float(profit_pct),
        "profit": float(profit),
        "net_total": float(net_total),
        "vat_percent": float(vat_pct),
        "vat": float(vat),
        "grand_total": float(grand_total),
        "item_count": len(computed),
        "unpriced_items": unpriced,
        "calculation_note": (
            "amount = quantity x rate (each line rounded to 2dp, then summed); "
            "overhead and contingency are % of subtotal; profit is % of "
            "(subtotal + overhead + contingency); VAT is % of the net total."
        ),
    }


@tool
def calculate_boq_totals(
    items: List[Dict[str, Any]],
    overhead_percent: float = 0,
    contingency_percent: float = 0,
    profit_margin_percent: float = 0,
    vat_percent: float = 0,
) -> str:
    """Compute every BOQ figure exactly. Use this for ALL BOQ arithmetic — never
    calculate line amounts, subtotals, margin, VAT or grand totals yourself.

    Args:
        items: BOQ line items. Each is an object with "description", "unit",
            "quantity" and "rate" (optionally "item_no" and "remarks").
            Leave "rate" as 0 for items the user has not priced yet.
        overhead_percent: Overhead as a percentage of the subtotal, e.g. 10 for 10%.
        contingency_percent: Contingency as a percentage of the subtotal.
        profit_margin_percent: Profit margin %, applied to subtotal + overhead + contingency.
        vat_percent: VAT %, applied to the net total.

    Returns:
        str: JSON with every line amount, the subtotal, each addition, and the
            grand total. Report these figures verbatim — do not re-derive them.
    """
    try:
        return json.dumps(compute_boq(
            items,
            overhead_percent=overhead_percent,
            contingency_percent=contingency_percent,
            profit_margin_percent=profit_margin_percent,
            vat_percent=vat_percent,
        ), indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e), "hint": "Fix the named field and call the tool again."}, indent=2)
