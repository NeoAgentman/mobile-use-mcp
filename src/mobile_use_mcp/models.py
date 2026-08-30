"""Validated data models shared by the Android core and MCP tools."""

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)


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


class RecordingState(StrEnum):
    """Lifecycle of the one recording owned by an Android session."""

    IDLE = "idle"
    RECORDING = "recording"
    STOPPING = "stopping"
    READY = "ready"
    FAILED = "failed"


# Descriptive aliases used by integrations that call this a recording
# lifecycle rather than a state.  Keeping one enum avoids divergent JSON
# values at the MCP boundary.
RecordingLifecycle = RecordingState
RecordingStatus = RecordingState


class DeviceInfo(StrictModel):
    serial: str = Field(min_length=1, max_length=256)
    state: DeviceState
    model: str | None = Field(default=None, max_length=256)
    product: str | None = Field(default=None, max_length=256)
    transport_id: str | None = Field(default=None, max_length=64)


_ANDROID_PACKAGE_PATTERN = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*\Z")

# These are deliberately named surfaces rather than a free-form escape hatch.
# A host may explicitly allow one of these well-known Android-owned surfaces,
# but model reasoning can never add an exception at execution time.
AndroidSystemSurface = Literal[
    "permission_dialog",
    "chooser",
    "system_ui",
    "launcher",
    "browser",
]

# Keep the same constraint in the JSON schema and in runtime validation. The
# anchors are intentional because Pydantic's string pattern checks are
# substring-based unless the pattern anchors itself.
AndroidPackageName = Annotated[
    StrictStr,
    StringConstraints(max_length=512, pattern=r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*$"),
]

_SYSTEM_SURFACE_ALIASES: dict[str, AndroidSystemSurface] = {
    "permission": "permission_dialog",
    "permissions": "permission_dialog",
    "permission_controller": "permission_dialog",
    "permission-dialog": "permission_dialog",
    "permission_dialog": "permission_dialog",
    "chooser": "chooser",
    "intent_chooser": "chooser",
    "intent-chooser": "chooser",
    "system_ui": "system_ui",
    "system-ui": "system_ui",
    "launcher": "launcher",
    "home": "launcher",
    "browser": "browser",
}

_SYSTEM_SURFACE_PACKAGES: dict[AndroidSystemSurface, frozenset[str]] = {
    "permission_dialog": frozenset(
        {
            "com.android.permissioncontroller",
            "com.google.android.permissioncontroller",
        }
    ),
    "chooser": frozenset(
        {
            "android",
            "com.android.intentresolver",
            "com.google.android.intentresolver",
        }
    ),
    "system_ui": frozenset({"com.android.systemui"}),
    "launcher": frozenset(
        {
            "com.android.launcher",
            "com.android.launcher3",
            "com.google.android.apps.nexuslauncher",
        }
    ),
    # A browser is not assumed to be an Android system package.  It is an
    # action surface that must be explicitly opted into because the URL tool
    # cannot know the user's default browser package before dispatch.
    "browser": frozenset(),
}


class PackagePolicySummary(StrictModel):
    """Safe, bounded description of the active session package policy."""

    enabled: bool
    mode: Literal["unrestricted", "allowlist"]
    allowed_packages: list[str] = Field(default_factory=list, max_length=100)
    allowed_system_packages: list[str] = Field(default_factory=list, max_length=100)
    allowed_system_surfaces: list[AndroidSystemSurface] = Field(
        default_factory=lambda: list[AndroidSystemSurface](),
        max_length=20,
    )


class PackagePolicy(StrictModel):
    """Deterministic allowlist for foreground and target Android packages.

    ``None`` at the session boundary means that policy enforcement is disabled.
    An instantiated policy, including one with empty lists, is restrictive and
    therefore denies every package until an explicit package or system surface
    is listed. Package matching is exact and case-insensitive.
    """

    allowed_packages: list[AndroidPackageName] = Field(
        default_factory=list,
        max_length=100,
        validation_alias=AliasChoices("allowed_packages", "allowed_app_packages"),
    )
    allowed_system_packages: list[AndroidPackageName] = Field(
        default_factory=list,
        max_length=100,
        validation_alias=AliasChoices("allowed_system_packages", "system_packages"),
    )
    allowed_system_surfaces: list[AndroidSystemSurface] = Field(
        default_factory=lambda: list[AndroidSystemSurface](),
        max_length=20,
        validation_alias=AliasChoices("allowed_system_surfaces", "system_surfaces"),
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_surface_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        source: dict[str, Any] = cast(dict[str, Any], value)
        if "allowed_packages" in source and "allowed_app_packages" in source:
            raise ValueError("allowed_packages and allowed_app_packages cannot both be provided")
        if "allowed_system_packages" in source and "system_packages" in source:
            raise ValueError(
                "allowed_system_packages and system_packages cannot both be provided"
            )
        if "allowed_system_surfaces" in source and "system_surfaces" in source:
            raise ValueError(
                "allowed_system_surfaces and system_surfaces cannot both be provided"
            )
        raw: Any = source.get("allowed_system_surfaces", source.get("system_surfaces"))
        if raw is None:
            return source
        if not isinstance(raw, list):
            return source
        items: list[Any] = cast(list[Any], raw)
        normalized: list[AndroidSystemSurface] = []
        seen: set[AndroidSystemSurface] = set()
        for item in items:
            if not isinstance(item, str):
                return source
            surface = _SYSTEM_SURFACE_ALIASES.get(item.strip().casefold())
            if surface is None:
                allowed = ", ".join(sorted(set(_SYSTEM_SURFACE_ALIASES.values())))
                raise ValueError(f"allowed_system_surfaces must be one of: {allowed}")
            if surface not in seen:
                normalized.append(surface)
                seen.add(surface)
        normalized_value: dict[str, Any] = dict(source)
        normalized_value["allowed_system_surfaces"] = normalized
        normalized_value.pop("system_surfaces", None)
        return normalized_value

    @field_validator("allowed_packages", "allowed_system_packages")
    @classmethod
    def validate_package_names(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            candidate = value.strip()
            if not candidate or not _ANDROID_PACKAGE_PATTERN.fullmatch(candidate):
                raise ValueError("must contain valid Android package names")
            folded = candidate.casefold()
            if folded not in seen:
                normalized.append(candidate)
                seen.add(folded)
        return normalized

    @model_validator(mode="after")
    def reject_overlapping_entries(self) -> "PackagePolicy":
        # Keeping app and system package lists disjoint makes the policy
        # summary auditable and avoids hiding a system exception in the app
        # allowlist. Matching remains exact in either case.
        app_packages = {value.casefold() for value in self.allowed_packages}
        overlap = [
            value for value in self.allowed_system_packages if value.casefold() in app_packages
        ]
        if overlap:
            raise ValueError("allowed_packages and allowed_system_packages must not overlap")
        return self

    def summary(self) -> PackagePolicySummary:
        """Return only policy inputs; never enumerate installed device apps."""

        return PackagePolicySummary(
            enabled=True,
            mode="allowlist",
            allowed_packages=list(self.allowed_packages),
            allowed_system_packages=list(self.allowed_system_packages),
            allowed_system_surfaces=list(self.allowed_system_surfaces),
        )

    @staticmethod
    def _matches(value: str, allowed: list[str]) -> bool:
        folded = value.casefold()
        return any(folded == candidate.casefold() for candidate in allowed)

    def allows_surface(self, surface: str) -> bool:
        """Whether one named system/action surface was explicitly enabled."""

        normalized = _SYSTEM_SURFACE_ALIASES.get(surface.strip().casefold())
        if normalized is None:
            return False
        if normalized in self.allowed_system_surfaces:
            return True
        # A caller may authorize a known Android-owned surface by naming its
        # exact package instead of using the convenience surface name.
        return any(
            self._matches(package, self.allowed_system_packages)
            for package in _SYSTEM_SURFACE_PACKAGES[normalized]
        )

    def allows_package(self, package: str | None, activity: str | None = None) -> bool:
        """Check one exact package or a package mapped to an allowed surface."""

        del activity  # Reserved for future surface-specific activity matching.
        if package is None or not package.strip():
            return False
        candidate = package.strip()
        if self._matches(candidate, self.allowed_packages):
            return True
        if self._matches(candidate, self.allowed_system_packages):
            return True
        for surface in self.allowed_system_surfaces:
            if candidate.casefold() in {
                item.casefold() for item in _SYSTEM_SURFACE_PACKAGES[surface]
            }:
                return True
        return False


# Descriptive aliases keep integrations from depending on one naming choice.
AndroidPackagePolicy = PackagePolicy
SessionPackagePolicy = PackagePolicy
AllowedPackagePolicy = PackagePolicy


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


class BoundsProvenance(StrictModel):
    """Capture identity attached to coordinates copied from an observation.

    Bounds are only safe to reuse while the session generation and screen
    revision that produced them are still current.  ``snapshot_id`` is also
    carried so an expired/evicted observation can produce a useful diagnostic.
    The values are metadata, not an encoding of an Android selector.
    """

    snapshot_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    screen_revision: int = Field(default=0, ge=0)

    @property
    def revision(self) -> int:
        return self.screen_revision


class SwipePercentage(StrictModel):
    """Normalized [0, 1] coordinates for a resolution-independent swipe."""

    start_x: float = Field(ge=0, le=1)
    start_y: float = Field(ge=0, le=1)
    end_x: float = Field(ge=0, le=1)
    end_y: float = Field(ge=0, le=1)


class Target(StrictModel):
    """A UI target with deterministic selector fallbacks."""

    element_ref: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Opaque temporary element reference returned by android_snapshot. It is bound to "
            "the originating session generation and screen revision."
        ),
    )
    snapshot_id: str | None = Field(
        default=None,
        max_length=128,
        description="Snapshot that supplied bounds or an element_ref, when available.",
    )
    bounds: Bounds | None = Field(
        default=None,
        description=(
            "Current on-screen bounds from android_snapshot. Bounds are tried first and their "
            "center is used for the action; refresh after the UI changes."
        ),
    )
    bounds_provenance: BoundsProvenance | None = Field(
        default=None,
        description=(
            "Session/generation/screen-revision provenance returned with snapshot bounds. "
            "Required for coordinate actions on a connected session."
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
        if (
            self.element_ref is None
            and self.bounds is None
            and not self.resource_id
            and not self.text
        ):
            raise ValueError(
                "At least one of element_ref, bounds, resource_id, or text is required"
            )
        return self


class UIElement(StrictModel):
    """A compact, platform-neutral representation of an Android UI node."""

    element_ref: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Opaque temporary reference for this observed node. It carries no raw selector "
            "data and expires with the observation's session generation/revision."
        ),
    )
    bounds_provenance: BoundsProvenance | None = Field(
        default=None,
        description=(
            "Capture identity for the node's bounds, if the node came from a session snapshot."
        ),
    )
    node_index: int | None = Field(
        default=None,
        ge=0,
        description="Flattened hierarchy position used only to disambiguate identical nodes.",
    )
    text: Annotated[
        str | None,
        StringConstraints(max_length=2_000, strip_whitespace=False),
    ] = None
    content_description: Annotated[
        str | None,
        StringConstraints(max_length=2_000, strip_whitespace=False),
    ] = None
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
    password: bool = Field(
        default=False,
        description=(
            "Whether Android accessibility identifies this element as a password field. "
            "Password fields are never content-verified by text equality."
        ),
    )

    @property
    def is_password_like(self) -> bool:
        """Return whether accessibility text must be treated as masked content."""

        if self.password:
            return True
        values = (
            self.class_name or "",
            self.resource_id or "",
            self.content_description or "",
        )
        return any("password" in value.casefold() for value in values)

    def has_semantic_content(self) -> bool:
        return bool(self.text or self.content_description or self.resource_id)

    def is_interactive(self) -> bool:
        return self.clickable or self.focusable or self.scrollable


class ForegroundApp(StrictModel):
    package: str | None = None
    activity: str | None = None


class AppInfo(StrictModel):
    """Deterministic metadata for one installed Android application.

    Android exposes different portions of package metadata on different API
    levels and OEM builds.  ``None`` is therefore intentional: it means the
    field is unknown on this device, rather than that the app has an empty
    label or is disabled.  The record remains useful for package selection
    even when optional metadata cannot be read.
    """

    package: str = Field(min_length=1, max_length=512)
    launcher_label: str | None = Field(
        default=None,
        max_length=2_000,
        validation_alias=AliasChoices("launcher_label", "label"),
    )
    launchable_activity: str | None = Field(
        default=None,
        max_length=1_000,
        validation_alias=AliasChoices("launchable_activity", "activity"),
    )
    is_system: bool | None = None
    is_third_party: bool | None = None
    enabled: bool | None = None
    metadata_status: Literal["available", "partial", "unavailable"] = "available"

    @property
    def label(self) -> str | None:
        """Compatibility alias for callers that call the label simply ``label``."""

        return self.launcher_label

    @property
    def package_name(self) -> str:
        """Compatibility alias for ADB clients that use ``package_name``."""

        return self.package

    @property
    def system(self) -> bool | None:
        """Compatibility alias for the system-app classification."""

        return self.is_system

    @property
    def third_party(self) -> bool | None:
        """Compatibility alias for the third-party classification."""

        return self.is_third_party

    @property
    def activity(self) -> str | None:
        """Compatibility alias for callers that call the activity simply ``activity``."""

        return self.launchable_activity


# ``AndroidApp`` is a descriptive alias used by embedders; both names share
# one schema and therefore serialize identically.
AndroidApp = AppInfo
AppRecord = AppInfo
InstalledApp = AppInfo


class AppInventory(StrictModel):
    """Core result of one deterministic Android package inventory read."""

    apps: list[AppInfo] = Field(default_factory=lambda: list[AppInfo]())
    metadata_status: Literal["available", "partial", "unavailable"] = "available"
    metadata_errors: list[str] = Field(default_factory=list, max_length=50)

    @property
    def metadata_unavailable(self) -> bool:
        """Whether optional package metadata was unavailable for this read."""

        return self.metadata_status == "unavailable"


class AppLaunchAttempt(StrictModel):
    attempt: int = Field(ge=1)
    outcome: str
    polls: int = Field(ge=0)
    foreground_app: ForegroundApp
    command_status: Literal["sent", "failed", "uncertain"] = "sent"
    effect_status: Literal["verified", "blocked", "unverified"] = "unverified"
    blocker: ForegroundApp | None = None
    transitions: list[ForegroundApp] = Field(
        default_factory=lambda: list[ForegroundApp](), max_length=50
    )
    error_code: str | None = Field(default=None, max_length=128)


class AppLaunchResult(StrictModel):
    requested_package: str | None = None
    launchable_activity: str | None = None
    command_status: Literal["sent", "failed", "uncertain"] = "sent"
    effect_status: Literal["verified", "blocked", "unverified"] = "verified"
    verification_available: bool = True
    stabilized: bool = False
    blockers: list[ForegroundApp] = Field(
        default_factory=lambda: list[ForegroundApp](), max_length=50
    )
    foreground_app: ForegroundApp
    attempts: list[AppLaunchAttempt]


class AppTerminationResult(StrictModel):
    """Postcondition-aware result of one exact Android force-stop request."""

    requested_package: str
    targeted_package: str
    command_status: Literal["sent", "failed", "uncertain"]
    effect_status: Literal["verified", "unverified"]
    effect_verified: bool
    verification_available: bool
    requires_observation: bool
    foreground_before: ForegroundApp | None = None
    foreground_after: ForegroundApp | None = None
    verification_reason: str | None = None


class ScreenSnapshot(StrictModel):
    """One immutable-in-storage Android observation.

    The capture context is deliberately part of the observation rather than
    being kept only in the session cache.  This lets callers safely page an
    identified observation while a later operation moves the screen on.
    ``None`` values keep the core model source-compatible with callers that
    construct a snapshot before a :class:`DeviceSession` has stamped it.
    """

    serial: str = Field(min_length=1, max_length=256)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    screenshot_png: bytes
    elements: list[UIElement]
    foreground_app: ForegroundApp
    session_id: str | None = Field(default=None, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    screen_revision: int = Field(
        default=0,
        ge=0,
        description="Monotonic screen revision within the connected session.",
    )
    captured_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp at which this observation was captured.",
    )
    snapshot_id: str | None = Field(default=None, max_length=128)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @property
    def revision(self) -> int:
        """Convenience alias for integrations that call it a revision."""

        return self.screen_revision

    @property
    def capture_time(self) -> datetime:
        """Compatibility alias for the public capture timestamp."""

        return self.captured_at


class SnapshotContext(StrictModel):
    """Capture provenance used to reject a late observation commit."""

    session_id: str | None = Field(default=None, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    screen_revision: int = Field(default=0, ge=0)

    @property
    def revision(self) -> int:
        return self.screen_revision


class ScreenshotCapture(StrictModel):
    """The hierarchy-free result of one native Android screenshot read."""

    serial: str = Field(min_length=1, max_length=256)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    screenshot_png: bytes


class ScreenshotResult(StrictModel):
    """Typed metadata accompanying a hierarchy-free MCP image."""

    success: bool
    operation_id: str = Field(min_length=1, max_length=128)
    serial: str = Field(min_length=1, max_length=256)
    session_id: str | None = Field(default=None, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    screen_revision: int = Field(default=0, ge=0)
    captured_at: datetime
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    image_format: Literal["png", "jpeg"]
    image_quality: int | None = Field(default=None, ge=1, le=100)
    image_lossless: bool
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    quality_notice: str
    lossless_fallback: dict[str, Any] | None = None
    hierarchy_collected: bool = False
    foreground_app_collected: bool = False


class SnapshotResult(ScreenshotResult):
    """Typed metadata for an immutable, pageable Android snapshot."""

    snapshot_id: str = Field(min_length=1, max_length=128)
    detail_level: Literal["compact", "full"]
    foreground_app: ForegroundApp
    warnings: list[str] = Field(default_factory=list, max_length=20)
    total_elements: int = Field(ge=0)
    returned_elements: int = Field(ge=0)
    element_count: int = Field(ge=0)
    truncated: bool
    next_offset: int | None = Field(default=None, ge=0)
    full_fallback: dict[str, Any] | None = None
    elements: list[UIElement] = Field(default_factory=lambda: [])


class SelectorAttempt(StrictModel):
    selector: str
    matched: bool
    error: str | None = None
    match_count: int = Field(default=0, ge=0)


class ResolvedTarget(StrictModel):
    bounds: Bounds
    selector: str
    matched_selector: str | None = None
    element: UIElement | None = None
    attempts: list[SelectorAttempt] = Field(default_factory=lambda: list[SelectorAttempt]())


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


class RecordingSegment(StrictModel):
    """Metadata for one host-local segment of an Android recording."""

    index: int = Field(ge=0)
    path: str = Field(min_length=1, max_length=4_096)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float = Field(default=0.0, ge=0)
    size_bytes: int = Field(default=0, ge=0)


class RecordingArtifact(StrictModel):
    """A completed, retained recording owned by the MCP server."""

    artifact_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^artifact-[0-9a-f]{32}$",
    )
    serial: str = Field(min_length=1, max_length=256)
    session_id: str | None = Field(default=None, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0)
    size_bytes: int = Field(ge=0)
    # ``path`` is the canonical artifact path.  The compatibility spellings
    # retain the pre-lifecycle recording response contract while all values
    # are generated by the server and remain below the configured root.
    path: str = Field(min_length=1, max_length=4_096)
    recording_path: str = Field(min_length=1, max_length=4_096)
    artifact_path: str = Field(min_length=1, max_length=4_096)
    segment_paths: list[str] = Field(default_factory=list, max_length=500)
    segment_count: int = Field(ge=1)
    segments: list[RecordingSegment] = Field(
        default_factory=lambda: list[RecordingSegment](), max_length=500
    )
    expires_at: datetime
    claimed: bool = False
    state: Literal["ready"] = "ready"
    warnings: list[str] = Field(default_factory=list, max_length=20)


class RecordingStatusSummary(StrictModel):
    """Bounded recording state included in Android session status."""

    state: RecordingState = RecordingState.IDLE
    artifact_id: str | None = Field(default=None, max_length=128)
    artifact: RecordingArtifact | None = None
    warnings: list[str] = Field(default_factory=list, max_length=20)


class RecordingStatusResult(ResultMappingMixin, StrictModel):
    """Typed result for recording transitions, retrieval, and status reads."""

    success: bool
    operation_id: str = Field(min_length=1, max_length=128)
    message: str
    state: RecordingState
    serial: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    artifact_id: str | None = Field(default=None, max_length=128)
    artifact: RecordingArtifact | None = None
    max_duration_seconds: int | None = Field(default=None, ge=1, le=1_800)
    bit_rate: int | None = Field(default=None, ge=100_000, le=100_000_000)
    duration_seconds: float | None = Field(default=None, ge=0)
    recording_path: str | None = Field(default=None, max_length=4_096)
    segment_paths: list[str] = Field(default_factory=list, max_length=500)
    segment_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    retrieved: bool = False
    error_code: str | None = Field(default=None, max_length=128)
    category: str | None = Field(default=None, max_length=64)
    retryable: bool | None = None
    suggestion: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    error: LifecycleError | None = None
    # Compatibility envelope for integrations that consumed the old generic
    # operation dictionary.
    data: dict[str, Any] = Field(default_factory=dict)


# A few embedders use ``RecordingResult`` or ``RecordingOperationResult`` for
# this envelope.  They are aliases, not separate schemas.
RecordingResult = RecordingStatusResult
RecordingOperationResult = RecordingStatusResult
RecordingStartResult = RecordingStatusResult
RecordingStopResult = RecordingStatusResult
RecordingRetrieveResult = RecordingStatusResult
RecordingArtifactMetadata = RecordingArtifact


class WaitResult(ResultMappingMixin, StrictModel):
    """Typed result for a bounded Android condition wait.

    A successful wait identifies the exact immutable observation that proved
    the condition.  Failure diagnostics intentionally retain only metadata
    about the last safe observation; they never include the text queried by a
    caller or a complete hierarchy.
    """

    success: bool
    operation_id: str = Field(min_length=1, max_length=128)
    condition: Literal["text", "element", "ui_change"]
    message: str
    poll_count: int = Field(ge=0)
    polls: int | None = Field(default=None, ge=0, description="Compatibility alias for poll_count.")
    elapsed_ms: float = Field(ge=0)
    timeout_seconds: float = Field(gt=0)
    poll_interval_seconds: float = Field(gt=0)
    snapshot_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    generation: int | None = Field(default=None, ge=1)
    screen_revision: int = Field(default=0, ge=0)
    captured_at: datetime | None = None
    matched_element: UIElement | None = None
    baseline_snapshot_id: str | None = Field(default=None, max_length=128)
    baseline_screen_revision: int | None = Field(default=None, ge=0)
    last_observation: dict[str, Any] | None = None
    last_safe_observation: dict[str, Any] | None = None
    last_error_stage: str | None = Field(default=None, max_length=64)
    last_error_code: str | None = Field(default=None, max_length=64)
    error_code: str | None = Field(default=None, max_length=64)
    suggestion: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


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


class AppListResult(LifecycleResult):
    """Typed, pageable result for deterministic Android App discovery."""

    query: str | None = None
    total: int = Field(default=0, ge=0)
    count: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    has_more: bool = False
    next_offset: int | None = Field(default=None, ge=0)
    metadata_status: Literal["available", "partial", "unavailable"] = "available"
    apps: list[AppInfo] = Field(default_factory=lambda: list[AppInfo]())
    # Kept as an inexpensive compatibility projection for clients that only
    # consumed package strings from the original android_list_apps tool.
    packages: list[str] = Field(default_factory=list)


class SessionConnection(StrictModel):
    """Core-level connection context returned after a device is ready."""

    session_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    state: SessionLifecycle
    device: DeviceInfo
    policy: PackagePolicySummary | None = None

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
    policy: PackagePolicySummary | None = None


class DeviceStatusResult(LifecycleResult):
    """Typed, privacy-safe view of the current Android session state."""

    active_operation_id: str | None = Field(default=None, max_length=128)
    screen_revision: int = Field(default=0, ge=0)
    current_snapshot_id: str | None = Field(default=None, max_length=128)
    snapshot_store: dict[str, int | float] = Field(default_factory=dict)
    last_error: LifecycleError | None = None
    policy: PackagePolicySummary = Field(
        default_factory=lambda: PackagePolicySummary(enabled=False, mode="unrestricted")
    )
    # Alias retained for callers that use the explicit package-policy name.
    package_policy: PackagePolicySummary | None = None
    recording: RecordingStatusSummary = Field(default_factory=RecordingStatusSummary)
    # Flat aliases keep status easy to consume for clients that do not unpack
    # nested summaries while ``recording`` remains the canonical grouping.
    recording_state: RecordingState = RecordingState.IDLE
    recording_artifact_id: str | None = Field(default=None, max_length=128)


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
