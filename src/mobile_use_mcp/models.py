"""Validated data models shared by the Android core and MCP tools."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base model that rejects misspelled or unsupported fields."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class DeviceState(StrEnum):
    DEVICE = "device"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"


class DeviceInfo(StrictModel):
    serial: str = Field(min_length=1, max_length=256)
    state: DeviceState
    model: str | None = Field(default=None, max_length=256)
    product: str | None = Field(default=None, max_length=256)
    transport_id: str | None = Field(default=None, max_length=64)


class Bounds(StrictModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def is_within(self, screen_width: int, screen_height: int) -> bool:
        center_x, center_y = self.center
        return 0 <= center_x < screen_width and 0 <= center_y < screen_height


class Target(StrictModel):
    """A UI target with deterministic selector fallbacks."""

    bounds: Bounds | None = None
    resource_id: str | None = Field(default=None, max_length=512)
    resource_id_index: int = Field(default=0, ge=0, le=10_000)
    text: str | None = Field(default=None, max_length=2_000)
    text_index: int = Field(default=0, ge=0, le=10_000)

    @model_validator(mode="after")
    def require_selector(self) -> "Target":
        if self.bounds is None and not self.resource_id and not self.text:
            raise ValueError("At least one of bounds, resource_id, or text is required")
        return self


class UIElement(StrictModel):
    """A compact, platform-neutral representation of an Android UI node."""

    text: str | None = Field(default=None, max_length=2_000)
    content_description: str | None = Field(default=None, max_length=2_000)
    resource_id: str | None = Field(default=None, max_length=512)
    class_name: str | None = Field(default=None, max_length=512)
    package: str | None = Field(default=None, max_length=512)
    bounds: Bounds | None = None
    clickable: bool = False
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    scrollable: bool = False
    selected: bool = False
    checked: bool | None = None

    def has_semantic_content(self) -> bool:
        return bool(self.text or self.content_description or self.resource_id)

    def is_interactive(self) -> bool:
        return self.clickable or self.focusable or self.scrollable


class ForegroundApp(StrictModel):
    package: str | None = None
    activity: str | None = None


class AppLaunchAttempt(StrictModel):
    attempt: int = Field(ge=1)
    outcome: str
    polls: int = Field(ge=0)
    foreground_app: ForegroundApp


class AppLaunchResult(StrictModel):
    foreground_app: ForegroundApp
    attempts: list[AppLaunchAttempt]


class ScreenSnapshot(StrictModel):
    serial: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    screenshot_png: bytes
    elements: list[UIElement]
    foreground_app: ForegroundApp


class SelectorAttempt(StrictModel):
    selector: str
    matched: bool
    error: str | None = None


class ResolvedTarget(StrictModel):
    bounds: Bounds
    selector: str
    element: UIElement | None = None
    attempts: list[SelectorAttempt] = []


class OperationResult(StrictModel):
    success: bool
    message: str
    error_code: str | None = None
    suggestion: str | None = None
    data: dict[str, Any] = {}
