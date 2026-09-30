"""Pydantic 请求模型（字段校验；几何语义校验在 geopoly 中）。"""

from __future__ import annotations

from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TrackPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time: str = Field(description="ISO 8601 时间（建议带 Z 的 UTC）")
    lon: float = Field(description="经度，[-180, 180]")
    lat: float = Field(description="纬度，[-90, 90]")

    @field_validator("lon", "lat", mode="before")
    @classmethod
    def _reject_bool(cls, v):
        if isinstance(v, bool):
            raise ValueError("坐标必须是数字，不能是布尔值")
        return v

    @field_validator("lon")
    @classmethod
    def _check_lon(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("经度必须是有限数字")
        if not -180.0 <= v <= 180.0:
            raise ValueError(f"经度越界: {v}（允许范围 [-180, 180]）")
        return v

    @field_validator("lat")
    @classmethod
    def _check_lat(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("纬度必须是有限数字")
        if not -90.0 <= v <= 90.0:
            raise ValueError(f"纬度越界: {v}（允许范围 [-90, 90]）")
        return v


class ProtectedArea(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: Union[str, int] = Field(description="保护区编号，原样返回")
    valid_from: str = Field(alias="valid_from", description="UTC 生效起点（含），ISO 8601")
    valid_to: str | None = Field(default=None, alias="valid_to",
                                 description="UTC 生效终点（不含），ISO 8601；空表示无限")
    geometry: dict[str, Any] = Field(description="GeoJSON Polygon 或 MultiPolygon")


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    areas: list[ProtectedArea] = Field(min_length=1, description="保护区列表，至少 1 个")
    track: list[TrackPoint] = Field(description="按时间排列的测线点")
