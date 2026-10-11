from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class Measurement(BaseModel):
    status: Literal[
        "MEASURED",
        "NO_MEASUREMENT",
        "EMPTY_GEOMETRY",
        "UNSUPPORTED",
        "INVALID_GEOMETRY",
        "MEASUREMENT_ERROR",
    ]
    area_m2: float | None = None
    length_m: float | None = None
    measurement_crs: str | None = None
    message: str | None = None
    warnings: list[str] = Field(default_factory=list)


class FeatureResult(BaseModel):
    index: int
    feature_id: str
    geometry_type: str | None
    geometry: dict[str, Any] | None
    crs: str
    properties: dict[str, Any]
    measurement: Measurement


class FileInfo(BaseModel):
    id: UUID
    filename: str
    format: Literal["KML", "SHAPEFILE"]
    feature_count: int
    crs: str
    crs_wkt: str
    status: Literal["COMPLETED"]
    uploaded_at: datetime
    size_bytes: int
    measured_feature_count: int
    warnings: list[str] = Field(default_factory=list)


class MeasurementPage(BaseModel):
    file_id: UUID
    total: int
    offset: int
    limit: int
    features: list[FeatureResult]
