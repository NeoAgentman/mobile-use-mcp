"""Validated data models shared by the Android core and MCP tools."""

from collections.abc import Iterator
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


class DoctorStatus(StrEnum):
    """Health state of one independent Android runtime capability probe."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class SessionLifecycle(StrEnum):
    """Observable lifecycle of the one Android session owned by the server."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    DEGRADED = "degraded"
    CLOSING = "closing"
    FAILED = "failed"


# These aliases keep the vocabulary convenient for callers that refer to the
# value as either a lifecycle or a session state.  They are aliases rather
# than separate enums, so JSON and equality semantics remain identical.
LifecycleState = SessionLifecycle
SessionState = SessionLifecycle
DeviceSessionState = SessionLifecycle


class DeviceInfo(StrictModel):
    serial: str = Field(min_length=1, max_length=256)
    state: DeviceState
    model: str | None = Field(default=None, max_length=256)
    product: str | None = Field(default=None, max_length=256)
    transport_id: str | None = Field(default=None, max_length=64)


class Bounds(StrictModel):
    x: int = Field(ge=0, description="Left coordinate in original device pixels.")
    y: int = Field(ge=0, description="Top coordinate in original device pixels.")
    width: int = Field(gt=0, description="Width in original device pixels.")
    height: int = Field(gt=0, description="Height in original device pixels.")

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    def is_within(self, screen_width: int, screen_height: int) -> bool:
        center_x, center_y = self.center
        return 0 <= center_x < screen_width and 0 <= center_y < screen_height


class Target(StrictModel):
    """A UI target with deterministic selector fallbacks."""

    bounds: Bounds | None = Field(
        default=None,
        description=(
            "Current on-screen bounds from android_snapshot. Bounds are tried first and their "
            "center is used for the action; refresh after the UI changes."
        ),
    )
    resource_id: str | None = Field(
        default=None,
        max_length=512,
        description="Exact resource_id from a current snapshot; tried after bounds.",
    )
    resource_id_index: int = Field(
        default=0,
        ge=0,
        le=10_000,
        description="Zero-based occurrence when multiple nodes share the resource_id.",
    )
    text: str | None = Field(
        default=None,
        max_length=2_000,
        description=(
            "Exact case-insensitive text selector tried after resource_id. It matches either a "
            "node's text or content_description; pass content_description values here. "
            "class_name and package are query filters, not Target fields."
        ),
    )
    text_index: int = Field(
        default=0,
        ge=0,
        le=10_000,
        description="Zero-based occurrence among exact text/content_description matches.",
    )

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


class LifecycleError(StrictModel):
    """Safe, machine-readable details for a lifecycle business failure."""

    code: str
    category: str
    retryable: bool
    message: str
    suggestion: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ResultMappingMixin:
    """Allow the new typed results to remain source-compatible with old dict callers."""

    def __getitem__(self, key: str) -> Any:
        return self.model_dump(mode="json")[key]  # type: ignore[attr-defined]

    def get(self, key: str, default: Any = None) -> Any:
        return self.model_dump(mode="json").get(key, default)  # type: ignore[attr-defined]

    def __contains__(self, key: object) -> bool:
        return key in self.model_dump(mode="json")  # type: ignore[attr-defined]

    def keys(self) -> Iterator[str]:
        return iter(self.model_dump(mode="json"))  # type: ignore[attr-defined]


class LifecycleResult(ResultMappingMixin, StrictModel):
    """Common typed envelope for Android session lifecycle tools."""

    success: bool
    operation_id: str = Field(min_length=1, max_length=128)
    message: str
    state: SessionLifecycle
    session_id: str | None = Field(default=None, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    device: DeviceInfo | None = None
    connected: bool = False
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    category: str | None = None
    retryable: bool | None = None
    suggestion: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    error: LifecycleError | None = None
    # Kept as a compatibility envelope for callers of the pre-lifecycle
    # dictionaries. New lifecycle fields remain available at the top level.
    data: dict[str, Any] = Field(default_factory=dict)


class SessionConnection(StrictModel):
    """Core-level connection context returned after a device is ready."""

    session_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    state: SessionLifecycle
    device: DeviceInfo

    @property
    def serial(self) -> str:
        """Compatibility convenience for callers that previously received DeviceInfo."""

        return self.device.serial


class DeviceListResult(LifecycleResult):
    """Typed result for Android device discovery and pagination."""

    total: int = Field(default=0, ge=0)
    count: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
    has_more: bool = False
    next_offset: int | None = Field(default=None, ge=0)
    devices: list[DeviceInfo] = Field(default_factory=lambda: [])


class DeviceConnectResult(LifecycleResult):
    """Typed result for a successful or failed Android connection attempt."""

    connected: bool = False


class DeviceStatusResult(LifecycleResult):
    """Typed, privacy-safe view of the current Android session state."""

    active_operation_id: str | None = Field(default=None, max_length=128)
    last_error: LifecycleError | None = None


class DeviceDisconnectResult(LifecycleResult):
    """Typed result for idempotent Android session close."""

    closed_session_id: str | None = Field(default=None, max_length=128)
    closed_generation: int | None = Field(default=None, ge=1)


class DoctorCheck(StrictModel):
    """One bounded, privacy-safe Android doctor probe result."""

    name: str = Field(min_length=1, max_length=64)
    status: DoctorStatus
    message: str = Field(max_length=1_000)
    duration_ms: float = Field(ge=0)
    details: dict[str, Any] = Field(default_factory=dict)


class DoctorResult(ResultMappingMixin, StrictModel):
    """Result of an Android environment diagnosis.

    ``success`` describes whether all required checks were ready.  The doctor
    itself still returns normally when a capability is degraded or unavailable
    so operators receive the complete independent matrix in one call.
    """

    success: bool
    operation_id: str = Field(min_length=1, max_length=128)
    overall_status: DoctorStatus
    message: str = Field(max_length=1_000)
    serial: str | None = Field(default=None, max_length=256)
    checks: list[DoctorCheck] = Field(default_factory=lambda: [])
    config: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    category: str | None = None
    retryable: bool | None = None
    suggestion: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        """Whether all doctor capabilities are ready."""

        return self.success
