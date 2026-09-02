"""Pydantic v2 models defining the FloorPlan contract the model must return.

Coordinate system: origin bottom-left, +x right, +y up, units = meters.
"""
from __future__ import annotations

from typing import List, Literal, Tuple

from pydantic import BaseModel, Field, model_validator

Point = Tuple[float, float]


class Wall(BaseModel):
    start: Point
    end: Point
    thickness: float = Field(default=0.15, gt=0, le=2.0)

    @model_validator(mode="after")
    def _non_degenerate(self) -> "Wall":
        if self.start == self.end:
            raise ValueError("wall start and end must differ")
        return self


class Opening(BaseModel):
    type: Literal["door", "window"]
    wall_index: int = Field(ge=0)
    # Distance along the wall from `start` to the opening's near edge (meters).
    offset: float = Field(ge=0)
    width: float = Field(gt=0, le=20.0)
    height: float = Field(gt=0, le=6.0)
    # Height of the window sill above the floor (meters). 0 for doors.
    sill: float = Field(default=0.0, ge=0, le=4.0)


class Room(BaseModel):
    name: str = Field(min_length=1)
    polygon: List[Point] = Field(min_length=3)


class Furniture(BaseModel):
    # Free-form string (not Literal) so an unexpected type never fails the whole
    # plan; the renderer maps known types and falls back for the rest.
    type: str = Field(min_length=1)
    position: Point  # center, same coordinate system as walls
    size: Point  # (width, depth) footprint in meters
    height: float = Field(default=0.8, gt=0, le=6.0)
    rotation: float = 0.0  # degrees, counter-clockwise
    room: str = ""


class FloorPlan(BaseModel):
    units: Literal["meters"] = "meters"
    wall_height: float = Field(default=2.7, gt=0, le=10.0)
    # Free text: how scale was determined, plus any assumptions made.
    scale_note: str = ""
    # Detailed natural-language description of the plan, used as context for the
    # Gemini render so the output stays faithful to the 2D CAD design.
    description: str = ""
    # Ordered polygon of the building's overall exterior footprint (the outer
    # wall corners, including diagonal/angled walls). Used to build one accurate
    # floor slab so the 3D stays true to the plan even if rooms are imperfect.
    outline: List[Point] = Field(default_factory=list)
    walls: List[Wall] = Field(min_length=1)
    openings: List[Opening] = Field(default_factory=list)
    rooms: List[Room] = Field(default_factory=list)
    # Optional; no longer rendered in three.js — Gemini handles furnishing.
    furniture: List[Furniture] = Field(default_factory=list)

    @model_validator(mode="after")
    def _openings_reference_real_walls(self) -> "FloorPlan":
        n = len(self.walls)
        for i, op in enumerate(self.openings):
            if op.wall_index >= n:
                raise ValueError(
                    f"openings[{i}].wall_index={op.wall_index} is out of range "
                    f"(only {n} walls)"
                )
        return self
