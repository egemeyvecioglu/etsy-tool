"""Pydantic schemas used by API endpoints and job processing."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class SelectionMode(str, Enum):
    """Supported listing selection strategies."""

    ALL_ACTIVE = "all_active"
    FILTER = "filter"
    LISTING_IDS = "listing_ids"


class AdjustmentType(str, Enum):
    """Supported price adjustment methods."""

    ABSOLUTE = "absolute"
    PERCENT = "percent"


class AdjustmentDirection(str, Enum):
    """Directional operator used to modify existing listing prices."""

    INCREASE = "increase"
    DECREASE = "decrease"


class RoundingMode(str, Enum):
    """Rounding behavior options for price recalculation."""

    STANDARD = "standard"
    BANKERS = "bankers"


class ListingSelection(BaseModel):
    """Selection payload for choosing target listings."""

    mode: SelectionMode
    filter_keyword: str | None = None
    listing_ids: list[str] | None = None

    @model_validator(mode="after")
    def validate_mode_specific_fields(self) -> "ListingSelection":
        """Ensure required fields are present for each selection mode."""

        if self.mode == SelectionMode.FILTER and not self.filter_keyword:
            raise ValueError("filter_keyword is required when mode=filter")
        if self.mode == SelectionMode.LISTING_IDS:
            if not self.listing_ids:
                raise ValueError("listing_ids is required when mode=listing_ids")
            self.listing_ids = [str(item).strip() for item in self.listing_ids if str(item).strip()]
            if not self.listing_ids:
                raise ValueError("listing_ids cannot be empty")
        return self


class AdjustmentConfig(BaseModel):
    """Core price adjustment instruction."""

    type: AdjustmentType
    direction: AdjustmentDirection = AdjustmentDirection.INCREASE
    value: Decimal

    @field_validator("value")
    @classmethod
    def validate_non_negative_value(cls, value: Decimal) -> Decimal:
        """Reject negative input values; direction controls add/subtract behavior."""

        if value < Decimal("0"):
            raise ValueError("adjustment value must be >= 0")
        return value


class JobOptions(BaseModel):
    """Optional behavior flags for a price adjustment job."""

    min_price: Decimal = Decimal("0.01")
    rounding: RoundingMode = RoundingMode.STANDARD
    dry_run: bool = False
    max_change_percent: Decimal | None = None

    @field_validator("min_price")
    @classmethod
    def validate_min_price(cls, value: Decimal) -> Decimal:
        """Disallow negative minimum price floors."""

        if value < Decimal("0"):
            raise ValueError("min_price must be >= 0")
        return value


class PriceAdjustJobRequest(BaseModel):
    """API request payload for creating a bulk price adjustment job."""

    selection: ListingSelection
    adjustment: AdjustmentConfig
    options: JobOptions = Field(default_factory=JobOptions)


class JobCreateResponse(BaseModel):
    """Response payload returned when a job is queued."""

    job_id: int
    status: str
    total_items: int


class JobSummaryResponse(BaseModel):
    """Aggregate job status payload used by API and polling UI."""

    id: int
    status: str
    type: str
    total_items: int
    completed_items: int
    succeeded_items: int
    failed_items: int
    skipped_items: int
    cancel_requested: bool
    created_at: str
    started_at: str | None
    finished_at: str | None


class JobItemResponse(BaseModel):
    """Per-listing status payload."""

    id: int
    listing_id: str
    status: str
    before_summary: dict[str, Any] | None
    after_summary: dict[str, Any] | None
    error: str | None
    updated_at: str
