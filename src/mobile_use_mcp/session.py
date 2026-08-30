"""The single-device Android session boundary used by the local stdio server.

The controller and its Android clients are deliberately kept behind this
module. The rest of the server can therefore reason about ownership and
lifecycle without knowing how ADB or uiautomator2 are initialized.
"""

import asyncio
import inspect
from collections.abc import Callable
from contextlib import suppress
from threading import Lock, RLock
from typing import Any, Never, cast
from uuid import uuid4

from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.devices import DeviceRegistry
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import (
    AndroidSystemSurface,
    BoundsProvenance,
    DeviceDisconnectResult,
    DeviceInfo,
    DeviceStatusResult,
    ForegroundApp,
    LifecycleError,
    PackagePolicy,
    PackagePolicySummary,
    ResolvedTarget,
    ScreenSnapshot,
    SessionConnection,
    SessionLifecycle,
    SnapshotContext,
    Target,
    UIElement,
)
from mobile_use_mcp.operations import OperationContext, OperationGateway, current_operation
from mobile_use_mcp.processes import run_blocking
from mobile_use_mcp.selectors import resolve_target
from mobile_use_mcp.snapshot import SnapshotStore

ControllerFactory = Callable[[str], AndroidController]


def _effective_operation(operation: OperationContext | None) -> OperationContext | None:
    """Use the gateway context propagated into callbacks and worker threads."""

    return operation if operation is not None else current_operation()


def _checkpoint(operation: OperationContext | None, stage: str = "execution") -> None:
    if operation is not None:
        operation.checkpoint(stage)


def _commit(
    operation: OperationContext | None,
    callback: Callable[[], Any],
    stage: str = "commit",
) -> Any:
    if operation is None:
        return callback()
    return operation.commit(callback, stage)


def _new_operation_id() -> str:
    return f"op-{uuid4().hex}"


def _new_session_id() -> str:
    return f"session-{uuid4().hex}"


def _safe_error_value(value: Any, depth: int = 0) -> object:
    if depth > 2:
        return None
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, int | float | bool) or value is None:
        return value
    if isinstance(value, list):
        items = cast(list[Any], value)
        return [_safe_error_value(item, depth + 1) for item in items[:50]]
    if isinstance(value, dict):
        nested = cast(dict[Any, Any], value)
        return {
            str(item_key): _safe_error_value(item_value, depth + 1)
            for item_key, item_value in list(nested.items())[:50]
        }
    return None


def _safe_error_details(error: MobileUseError) -> dict[str, object]:
    """Keep error data bounded and free of exception implementation details."""

    return {key: _safe_error_value(value) for key, value in error.data.items()}


def _lifecycle_error(error: MobileUseError) -> LifecycleError:
    return LifecycleError(
        code=error.code.value,
        category=error.category.value,
        retryable=error.retryable,
        message=error.message,
        suggestion=error.suggestion,
        details=_safe_error_details(error),
    )


class DeviceSession:
    """Own one Android controller and expose explicit lifecycle transitions.

    ``connect`` and ``disconnect`` retain synchronous core methods because the
    underlying Android adapters are synchronous. ``close``/``aclose`` are the
    awaitable boundary used by server shutdown and delegate to the exact same
    cleanup implementation as explicit disconnect.
    """

    def __init__(
        self,
        registry: DeviceRegistry | None = None,
        controller_factory: ControllerFactory = AndroidController,
        *,
        config: RuntimeConfig | None = None,
        snapshot_store: SnapshotStore | None = None,
        policy: PackagePolicy | dict[str, Any] | None = None,
        package_policy: PackagePolicy | dict[str, Any] | None = None,
    ):
        self.config = config or RuntimeConfig.defaults()
        self.registry = registry or DeviceRegistry(config=self.config)
        if policy is not None and package_policy is not None:
            raise ValueError("policy and package_policy cannot both be provided")
        configured_policy = package_policy if package_policy is not None else policy
        self._policy = self._coerce_policy(configured_policy)
        # Keep injected factories source-compatible for deterministic core
        # tests.  The production default receives the same frozen config as
        # discovery and therefore cannot drift to another ADB endpoint.
        controller_impl: ControllerFactory = (
            controller_factory
            if controller_factory is not AndroidController
            else lambda serial: AndroidController(serial, config=self.config)
        )
        self.controller_factory = controller_impl
        self._device: DeviceInfo | None = None
        self._controller: AndroidController | None = None
        self._snapshot_store = snapshot_store or SnapshotStore(
            max_entries=self.config.snapshot_max_entries,
            max_bytes=self.config.snapshot_max_bytes,
            ttl_seconds=self.config.snapshot_ttl_seconds,
        )
        # ``_snapshot_id`` and ``_snapshot`` remain compatibility pointers to
        # the latest committed observation.  The store itself retains older
        # immutable observations until TTL or capacity eviction.
        self._snapshot_id: str | None = None
        self._snapshot: ScreenSnapshot | None = None
        self._legacy_snapshot_input: ScreenSnapshot | None = None
        self._current_snapshot_revision_managed = False
        self._screen_revision = 0

        self._state = SessionLifecycle.DISCONNECTED
        self._session_id: str | None = None
        self._generation: int | None = None
        self._generation_counter = 0
        self._active_operation_id: str | None = None
        self._last_error: LifecycleError | None = None
        # An explicit close request is observable by long-running read waits
        # even while the close operation is queued behind the current FIFO
        # item. This lets condition waits terminate promptly without allowing
        # a late observation commit after the caller has requested disconnect.
        self._disconnect_requested = False
        self._state_lock = RLock()
        self._lifecycle_lock = Lock()

        self.operation_gateway = OperationGateway(
            operation_timeout_seconds=self.config.operation_timeout_seconds,
            queue_timeout_seconds=self.config.queue_timeout_seconds,
            on_start=self._operation_started,
            on_finish=self._operation_finished,
        )
        # ``gateway`` is intentionally a short public alias for embedders and
        # tests that use DeviceSession as the coordination seam directly.
        self.gateway = self.operation_gateway

        # Kept for source compatibility with integrations that acquired the
        # old action lock directly.  Public MCP tools use operation_gateway;
        # retaining this lock does not create a second scheduling path.
        self.write_lock = asyncio.Lock()

    def _operation_started(self, operation: OperationContext) -> None:
        with self._state_lock:
            self._active_operation_id = operation.operation_id

    def _operation_finished(self, operation: OperationContext) -> None:
        with self._state_lock:
            if self._active_operation_id == operation.operation_id:
                self._active_operation_id = None
            # Connect/disconnect detach their old references before entering
            # a potentially blocking Android adapter.  If the gateway cancels
            # that adapter, leave the session in a safe terminal state rather
            # than exposing a permanent connecting/closing lifecycle.
            if not operation.active and operation.name == "connect":
                if self._state == SessionLifecycle.CONNECTING:
                    self._state = SessionLifecycle.FAILED
                    self._last_error = _lifecycle_error(
                        operation.state_error(cancelled=operation.cancelled)
                    )
                    self._device = None
                    self._controller = None
                    self._session_id = None
                    self._generation = None
                    self._clear_snapshot_unchecked()
            elif (
                not operation.active
                and operation.name == "disconnect"
                and self._state == SessionLifecycle.CLOSING
            ):
                self._state = SessionLifecycle.DISCONNECTED
                self._last_error = _lifecycle_error(
                    operation.state_error(cancelled=operation.cancelled)
                )

    async def run_operation(
        self,
        name: str,
        callback: Callable[[OperationContext], Any],
        *,
        operation_id: str | None = None,
        timeout_seconds: float | None = None,
        retry_safe: bool = False,
        mutating: bool = True,
    ) -> Any:
        """Run one Android operation through the session's FIFO gateway."""

        return await self.operation_gateway.run(
            name,
            callback,
            operation_id=operation_id,
            timeout_seconds=timeout_seconds,
            retry_safe=retry_safe,
            mutating=mutating,
        )

    async def execute_operation(self, *args: Any, **kwargs: Any) -> Any:
        """Compatibility alias for callers that name the seam ``execute``."""

        return await self.run_operation(*args, **kwargs)

    @property
    def device(self) -> DeviceInfo | None:
        with self._state_lock:
            return self._device

    @property
    def controller(self) -> AndroidController | None:
        with self._state_lock:
            return self._controller

    @property
    def state(self) -> SessionLifecycle:
        with self._state_lock:
            # A few existing integrations set ``_device`` while replacing the
            # old SessionManager in tests. Treat that as the legacy equivalent
            # of a ready selection without claiming a controller exists.
            if self._state == SessionLifecycle.DISCONNECTED and self._device is not None:
                return SessionLifecycle.READY
            return self._state

    @property
    def lifecycle_state(self) -> SessionLifecycle:
        return self.state

    @property
    def lifecycle(self) -> SessionLifecycle:
        return self.state

    @property
    def session_id(self) -> str | None:
        with self._state_lock:
            return self._session_id

    @staticmethod
    def _coerce_policy(
        value: PackagePolicy | dict[str, Any] | None,
    ) -> PackagePolicy | None:
        if value is None:
            return None
        if isinstance(value, PackagePolicy):
            return value.model_copy(deep=True)
        return PackagePolicy.model_validate(value)

    @property
    def policy(self) -> PackagePolicy | None:
        """Return a copy of the restrictive package policy, if configured."""

        with self._state_lock:
            return self._policy.model_copy(deep=True) if self._policy is not None else None

    @property
    def package_policy(self) -> PackagePolicy | None:
        """Compatibility alias for the session package-scope policy."""

        return self.policy

    def policy_summary(self) -> PackagePolicySummary:
        """Return a safe policy summary without reading installed apps."""

        policy = self.policy
        if policy is None:
            return PackagePolicySummary(
                enabled=False,
                mode="unrestricted",
            )
        return policy.summary()

    async def check_package_policy(
        self,
        *,
        action: str,
        controller: Any | None = None,
        target_package: str | None = None,
        target_package_required: bool = False,
        target_surface: str | None = None,
        operation: OperationContext | None = None,
    ) -> ForegroundApp | None:
        """Authorize one write against the current and target package.

        This is the single policy seam used by all state-changing MCP tools.
        It runs before any adapter command, treats a missing/failed foreground
        read as a deny, and never accepts an exception based on host-agent
        reasoning. An unrestricted session returns without probing foreground
        state to preserve the historical optional-policy behavior.
        """

        active_operation = _effective_operation(operation)
        _checkpoint(active_operation, "package_policy")
        policy = self.policy
        if policy is None:
            return None
        if controller is None:
            controller = self.require_controller(operation=active_operation)

        def foreground_error(reason: str, cause_code: str | None = None) -> MobileUseError:
            details: dict[str, Any] = {
                "stage": "package_policy",
                "action": action,
                "reason": reason,
                "foreground_state": "unavailable",
                "policy": policy.summary().model_dump(mode="json"),
            }
            if cause_code is not None:
                details["cause_code"] = cause_code
            return MobileUseError(
                ErrorCode.PACKAGE_POLICY_FOREGROUND_UNAVAILABLE,
                "Android foreground package could not be verified for the session policy.",
                "Retry after confirming the device is connected and responsive.",
                data=details,
                retryable_override=True,
            )

        reader = getattr(controller, "get_foreground_app", None)
        if not callable(reader):
            raise foreground_error("foreground_reader_unavailable")
        try:
            value = reader()
            if inspect.isawaitable(value):
                value = await value
            foreground = (
                value
                if isinstance(value, ForegroundApp)
                else ForegroundApp.model_validate(value)
            )
        except MobileUseError as error:
            raise foreground_error("foreground_read_failed", error.code.value) from error
        except Exception as error:
            del error  # Do not expose adapter exception text to the host.
            raise foreground_error("foreground_read_failed") from None

        assert foreground is not None
        if not foreground.package:
            raise MobileUseError(
                ErrorCode.PACKAGE_POLICY_DENIED,
                "The Android package policy denied the action because no foreground package was "
                "available.",
                "Wait for an Android surface to become foreground, then retry the action.",
                data={
                    "stage": "package_policy",
                    "action": action,
                    "reason": "missing_foreground",
                    "foreground_state": "empty",
                    "current_package": None,
                    "target_package": target_package,
                    "target_surface": target_surface,
                    "policy": policy.summary().model_dump(mode="json"),
                },
                retryable_override=False,
            )
        if not policy.allows_package(foreground.package, foreground.activity):
            raise MobileUseError(
                ErrorCode.PACKAGE_POLICY_DENIED,
                "The Android package policy denied the action for the current foreground package.",
                "Use an explicitly allowed package or system surface, then retry the action.",
                data={
                    "stage": "package_policy",
                    "action": action,
                    "reason": "current_package_denied",
                    "foreground_state": "occupied",
                    "current_package": foreground.package,
                    "current_activity": foreground.activity,
                    "target_package": target_package,
                    "target_surface": target_surface,
                    "policy": policy.summary().model_dump(mode="json"),
                },
                retryable_override=False,
            )
        if target_package_required and not target_package:
            raise MobileUseError(
                ErrorCode.PACKAGE_POLICY_DENIED,
                "The Android package policy denied the action because its target package could not "
                "be verified.",
                "Refresh the Android UI hierarchy and retry with a target whose package is known.",
                data={
                    "stage": "package_policy",
                    "action": action,
                    "reason": "target_package_unavailable",
                    "foreground_state": "occupied",
                    "current_package": foreground.package,
                    "target_package": target_package,
                    "target_surface": target_surface,
                    "policy": policy.summary().model_dump(mode="json"),
                },
                retryable_override=False,
            )
        if target_package is not None and not policy.allows_package(target_package):
            raise MobileUseError(
                ErrorCode.PACKAGE_POLICY_DENIED,
                "The Android package policy denied the action for its target package.",
                "Use an explicitly allowed target package, then retry the action.",
                data={
                    "stage": "package_policy",
                    "action": action,
                    "reason": "target_package_denied",
                    "foreground_state": "occupied",
                    "current_package": foreground.package,
                    "target_package": target_package,
                    "target_surface": target_surface,
                    "policy": policy.summary().model_dump(mode="json"),
                },
                retryable_override=False,
            )
        if target_surface is not None and not policy.allows_surface(target_surface):
            raise MobileUseError(
                ErrorCode.PACKAGE_POLICY_DENIED,
                "The Android package policy denied the requested Android system surface.",
                "Declare the system surface explicitly in the connection policy, then retry.",
                data={
                    "stage": "package_policy",
                    "action": action,
                    "reason": "system_surface_denied",
                    "foreground_state": "occupied",
                    "current_package": foreground.package,
                    "target_package": target_package,
                    "target_surface": target_surface,
                    "policy": policy.summary().model_dump(mode="json"),
                },
                retryable_override=False,
            )
        _checkpoint(active_operation, "package_policy")
        return foreground

    # Descriptive aliases used by embedders and tests that name the seam as an
    # authorization boundary rather than a policy check.
    authorize_action = check_package_policy
    enforce_package_policy = check_package_policy

    @property
    def generation(self) -> int | None:
        with self._state_lock:
            return self._generation

    @property
    def last_generation(self) -> int | None:
        with self._state_lock:
            return self._generation_counter or None

    @property
    def screen_revision(self) -> int:
        """Current monotonic screen revision for the active session."""

        with self._state_lock:
            return self._screen_revision

    @property
    def revision(self) -> int:
        """Compatibility alias for :attr:`screen_revision`."""

        return self.screen_revision

    @property
    def current_snapshot_id(self) -> str | None:
        with self._state_lock:
            return self._snapshot_id

    @property
    def snapshot_store(self) -> SnapshotStore:
        """Expose the bounded store for diagnostics and embedders."""

        return self._snapshot_store

    def observation_context(self) -> SnapshotContext:
        """Capture the session context before starting a possibly slow read."""

        with self._state_lock:
            return SnapshotContext(
                session_id=self._session_id,
                generation=self._generation,
                screen_revision=self._screen_revision,
            )

    # ``snapshot_context`` is intentionally a named alias: callers that use
    # either observation terminology can share the same race-safe seam.
    snapshot_context = observation_context

    @property
    def active_operation_id(self) -> str | None:
        with self._state_lock:
            return self._active_operation_id

    @property
    def last_error(self) -> LifecycleError | None:
        with self._state_lock:
            return self._last_error

    @property
    def connected(self) -> bool:
        with self._state_lock:
            return (
                self._state == SessionLifecycle.READY
                and self._device is not None
                and self._controller is not None
            )

    @property
    def disconnect_requested(self) -> bool:
        """Whether an explicit asynchronous close is waiting to run."""

        with self._state_lock:
            return self._disconnect_requested

    def new_operation_id(self) -> str:
        """Allocate an opaque ID for a public operation."""

        return _new_operation_id()

    def list_devices(
        self,
        *,
        operation_id: str | None = None,
        operation: OperationContext | None = None,
    ) -> list[DeviceInfo]:
        """List every ADB device, preserving offline and unauthorized states."""

        active_operation = _effective_operation(operation)
        effective_operation_id = operation_id or (
            active_operation.operation_id if active_operation is not None else _new_operation_id()
        )
        _checkpoint(active_operation)
        with self._lifecycle_lock:
            with self._state_lock:
                self._active_operation_id = effective_operation_id
            try:
                devices = self.registry.list_devices()
                _checkpoint(active_operation)
                return devices
            except MobileUseError as error:
                _commit(
                    active_operation,
                    lambda error=error: self._set_last_error(_lifecycle_error(error)),
                    "error_commit",
                )
                raise
            finally:
                if active_operation is None:
                    with self._state_lock:
                        if self._active_operation_id == effective_operation_id:
                            self._active_operation_id = None

    async def alist_devices(
        self,
        *,
        operation_id: str | None = None,
        operation: OperationContext | None = None,
    ) -> list[DeviceInfo]:
        """Cancellable device discovery for the async MCP boundary."""

        active_operation = _effective_operation(operation)
        effective_operation_id = operation_id or (
            active_operation.operation_id if active_operation is not None else _new_operation_id()
        )
        _checkpoint(active_operation)
        with self._state_lock:
            self._active_operation_id = effective_operation_id
        try:
            # Preserve the deterministic fake seam used by embedders that
            # replace the legacy sync method, while production registries use
            # the cancellable process-runner path.
            if (
                "list_devices" not in vars(self.registry)
                and type(self.registry).list_devices is DeviceRegistry.list_devices
            ):
                devices = await self.registry.alist_devices()
            else:
                devices = await run_blocking(
                    self.registry.list_devices,
                    timeout_seconds=self.config.subprocess_timeout_seconds,
                )
            _checkpoint(active_operation)
            return devices
        except MobileUseError as error:
            _commit(
                active_operation,
                lambda error=error: self._set_last_error(_lifecycle_error(error)),
                "error_commit",
            )
            raise
        finally:
            if active_operation is None:
                with self._state_lock:
                    if self._active_operation_id == effective_operation_id:
                        self._active_operation_id = None

    def _set_last_error(self, error: LifecycleError | None) -> None:
        with self._state_lock:
            self._last_error = error

    def _resolve_connect_policy(
        self,
        policy: PackagePolicy | dict[str, Any] | None,
        package_policy: PackagePolicy | dict[str, Any] | None,
        allowed_packages: list[str] | None,
        allowed_app_packages: list[str] | None,
        allowed_system_packages: list[str] | None,
        system_packages: list[str] | None,
        allowed_system_surfaces: list[str] | None,
        system_surfaces: list[str] | None,
        allow_unrestricted: bool,
    ) -> PackagePolicy | None:
        """Normalize one connection's strict policy declaration.

        A restrictive policy is retained across reconnects unless the caller
        explicitly opts into ``allow_unrestricted``. This prevents reconnect
        from becoming an authorization bypass while preserving an unrestricted
        default for the first connection.
        """

        policy_values = [value for value in (policy, package_policy) if value is not None]
        if len(policy_values) > 1:
            raise ValueError("policy and package_policy cannot both be provided")
        direct_values = {
            "allowed_packages": allowed_packages,
            "allowed_app_packages": allowed_app_packages,
            "allowed_system_packages": allowed_system_packages,
            "system_packages": system_packages,
            "allowed_system_surfaces": allowed_system_surfaces,
            "system_surfaces": system_surfaces,
        }
        supplied_direct = {key: value for key, value in direct_values.items() if value is not None}
        if len({
            key
            for key in ("allowed_packages", "allowed_app_packages")
            if direct_values[key] is not None
        }) > 1:
            raise ValueError("allowed_packages and allowed_app_packages cannot both be provided")
        if len({
            key
            for key in ("allowed_system_packages", "system_packages")
            if direct_values[key] is not None
        }) > 1:
            raise ValueError(
                "allowed_system_packages and system_packages cannot both be provided"
            )
        if len({
            key
            for key in ("allowed_system_surfaces", "system_surfaces")
            if direct_values[key] is not None
        }) > 1:
            raise ValueError(
                "allowed_system_surfaces and system_surfaces cannot both be provided"
            )
        if policy_values and supplied_direct:
            raise ValueError("policy cannot be combined with direct package allowlists")
        if allow_unrestricted and (policy_values or supplied_direct):
            raise ValueError("allow_unrestricted cannot be combined with a package policy")
        if policy_values:
            return self._coerce_policy(policy_values[0])
        if supplied_direct:
            return PackagePolicy(
                allowed_packages=(allowed_packages or allowed_app_packages or []),
                allowed_system_packages=(allowed_system_packages or system_packages or []),
                allowed_system_surfaces=cast(
                    list[AndroidSystemSurface],
                    (allowed_system_surfaces or system_surfaces or []),
                ),
            )
        if allow_unrestricted:
            return None
        # Reconnects inherit a previous restrictive policy by default. A
        # fresh DeviceSession still starts unrestricted because ``_policy`` is
        # ``None`` until a caller opts in.
        return self.policy

    def connect(
        self,
        serial: str | None = None,
        *,
        operation_id: str | None = None,
        operation: OperationContext | None = None,
        policy: PackagePolicy | dict[str, Any] | None = None,
        package_policy: PackagePolicy | dict[str, Any] | None = None,
        allowed_packages: list[str] | None = None,
        allowed_app_packages: list[str] | None = None,
        allowed_system_packages: list[str] | None = None,
        system_packages: list[str] | None = None,
        allowed_system_surfaces: list[str] | None = None,
        system_surfaces: list[str] | None = None,
        allow_unrestricted: bool = False,
    ) -> SessionConnection:
        """Connect one selected device and return a new session generation.

        A reconnect first detaches the previous controller. If any stage of
        the new attempt fails, both the newly-created controller and all prior
        session references are cleared, leaving no connected controller behind.
        """

        requested_policy = self._resolve_connect_policy(
            policy,
            package_policy,
            allowed_packages,
            allowed_app_packages,
            allowed_system_packages,
            system_packages,
            allowed_system_surfaces,
            system_surfaces,
            allow_unrestricted,
        )
        active_operation = _effective_operation(operation)
        effective_operation_id = operation_id or (
            active_operation.operation_id if active_operation is not None else _new_operation_id()
        )
        _checkpoint(active_operation)
        with self._lifecycle_lock:
            with self._state_lock:
                old_controller = _commit(
                    active_operation,
                    lambda: self._prepare_connect_locked(effective_operation_id),
                    "session_commit",
                )
            if old_controller is not None:
                self._safe_disconnect(old_controller)
            try:
                _checkpoint(active_operation)
                selected = self.registry.select(serial)
                _checkpoint(active_operation)
                controller = self.controller_factory(selected.serial)
                try:
                    _checkpoint(active_operation)
                    controller.android_client.connect()
                except Exception:
                    # The Android client can have acquired a transport before
                    # reporting an error. Always release it, but preserve the
                    # original exception for the core caller.
                    self._safe_disconnect(controller)
                    raise
            except MobileUseError as error:
                if active_operation is None or active_operation.active:
                    _commit(
                        active_operation,
                        lambda error=error: self._mark_connection_failed(error),
                        "error_commit",
                    )
                raise
            except Exception:
                failure = MobileUseError(
                    ErrorCode.OPERATION_FAILED,
                    "Failed to initialize the Android automation connection.",
                    "Check `adb devices -l`, unlock the device, and try again.",
                )
                if active_operation is None or active_operation.active:
                    _commit(
                        active_operation,
                        lambda: self._mark_connection_failed(failure),
                        "error_commit",
                    )
                raise
            else:
                with self._state_lock:
                    try:
                        result = _commit(
                            active_operation,
                            lambda: self._commit_connection_locked(
                                selected,
                                controller,
                                policy=requested_policy,
                            ),
                            "session_commit",
                        )
                    except MobileUseError:
                        # A deadline/cancellation can win just after the
                        # adapter connected.  Release the new controller but
                        # never publish it as the active session.
                        self._safe_disconnect(controller)
                        raise
                return result
            finally:
                if active_operation is None:
                    with self._state_lock:
                        if self._active_operation_id == effective_operation_id:
                            self._active_operation_id = None

    def _prepare_connect_locked(self, operation_id: str) -> AndroidController | None:
        self._active_operation_id = operation_id
        self._state = SessionLifecycle.CONNECTING
        self._last_error = None
        return self._detach_controller_locked()

    def _mark_connection_failed(self, error: MobileUseError) -> None:
        with self._state_lock:
            self._state = SessionLifecycle.FAILED
            self._last_error = _lifecycle_error(error)
            self._device = None
            self._controller = None
            self._screen_revision = 0
            self._clear_snapshot_unchecked()

    def _commit_connection_locked(
        self,
        selected: DeviceInfo,
        controller: AndroidController,
        *,
        policy: PackagePolicy | None,
    ) -> SessionConnection:
        self._generation_counter += 1
        self._generation = self._generation_counter
        self._session_id = _new_session_id()
        self._device = selected
        self._controller = controller
        self._policy = policy.model_copy(deep=True) if policy is not None else None
        self._screen_revision = 0
        self._state = SessionLifecycle.READY
        self._last_error = None
        self._clear_snapshot_unchecked()
        return SessionConnection(
            session_id=self._session_id,
            generation=self._generation,
            state=self._state,
            device=selected,
            policy=self.policy_summary(),
        )

    def _detach_controller_locked(self) -> AndroidController | None:
        """Release the current controller while the lifecycle lock is held."""

        controller = self._controller
        self._controller = None
        self._device = None
        self._session_id = None
        self._generation = None
        self._screen_revision = 0
        self._clear_snapshot_unchecked()
        return controller

    @staticmethod
    def _safe_disconnect(controller: AndroidController) -> None:
        with suppress(Exception):
            controller.disconnect()

    def store_snapshot(
        self,
        snapshot: ScreenSnapshot,
        *,
        operation: OperationContext | None = None,
        expected_context: SnapshotContext | None = None,
        expected_session_id: str | None = None,
        expected_generation: int | None = None,
        expected_revision: int | None = None,
        expected_screen_revision: int | None = None,
    ) -> str:
        """Commit one observation if its capture context is still current.

        The expected context is optional for compatibility with direct core
        callers.  Public snapshot capture supplies it immediately before the
        adapter read; a later screen mutation then rejects that late result
        instead of allowing it to become the current observation.
        """

        active_operation = _effective_operation(operation)
        alias_revision = (
            expected_screen_revision if expected_screen_revision is not None else expected_revision
        )

        def store() -> str:
            with self._state_lock:
                if self._disconnect_requested:
                    raise MobileUseError(
                        ErrorCode.DEVICE_DISCONNECTED,
                        "The Android session is closing; the observation cannot be committed.",
                        (
                            "Wait for android_disconnect to finish, then observe again after "
                            "reconnecting."
                        ),
                    )
                current = SnapshotContext(
                    session_id=self._session_id,
                    generation=self._generation,
                    screen_revision=self._screen_revision,
                )
                context_mismatch = expected_context is not None and current != expected_context
                alias_mismatch = expected_context is None and (
                    (expected_session_id is not None and current.session_id != expected_session_id)
                    or (
                        expected_generation is not None
                        and current.generation != expected_generation
                    )
                    or (alias_revision is not None and current.screen_revision != alias_revision)
                )
                if context_mismatch or alias_mismatch:
                    expected_sid = (
                        expected_context.session_id
                        if expected_context is not None
                        else expected_session_id
                    )
                    expected_gen = (
                        expected_context.generation
                        if expected_context is not None
                        else expected_generation
                    )
                    expected_rev = (
                        expected_context.screen_revision
                        if expected_context is not None
                        else alias_revision
                    )
                    raise MobileUseError(
                        ErrorCode.SNAPSHOT_STALE,
                        "The Android observation finished after the screen changed.",
                        "Discard this observation and call android_snapshot again.",
                        data={
                            "expected_session_id": expected_sid,
                            "current_session_id": current.session_id,
                            "expected_generation": expected_gen,
                            "current_generation": current.generation,
                            "expected_screen_revision": expected_rev,
                            "current_screen_revision": current.screen_revision,
                        },
                    )

                stamped = snapshot.model_copy(deep=True)
                stamped.session_id = current.session_id
                stamped.generation = current.generation
                stamped.screen_revision = current.screen_revision
                assigned_snapshot_id = f"s-{uuid4().hex}"
                stamped.snapshot_id = assigned_snapshot_id
                # Element references are random capabilities, never encoded
                # selectors. Each reference carries provenance in a separate
                # typed field so copied coordinates cannot silently outlive
                # their session generation or screen revision.
                for element in stamped.elements:
                    element.element_ref = f"el-{uuid4().hex}"
                    element.bounds_provenance = BoundsProvenance(
                        snapshot_id=assigned_snapshot_id,
                        session_id=current.session_id,
                        generation=current.generation,
                        screen_revision=current.screen_revision,
                    )
                # The store owns the opaque identifier.  A caller-provided ID
                # must never allow an observation to overwrite another entry.
                # The generated ID is supplied explicitly so its value is
                # included in the store's byte accounting.

                # Preserve the identity behavior of the old unbound core API
                # only for the exact same object stored twice.  MCP captures
                # are always session-bound and take the immutable path.
                repeated_legacy = (
                    current.session_id is None
                    and expected_context is None
                    and expected_session_id is None
                    and expected_generation is None
                    and alias_revision is None
                    and self._legacy_snapshot_input is snapshot
                    and self._snapshot_id is not None
                )
                if repeated_legacy:
                    previous_id = self._snapshot_id
                    assert previous_id is not None
                    self._snapshot_store.forget(previous_id)
                    stamped = snapshot
                    stamped.session_id = None
                    stamped.generation = None
                    stamped.screen_revision = 0
                    stored = self._snapshot_store.put(
                        stamped,
                        snapshot_id=assigned_snapshot_id,
                        clone=False,
                    )
                else:
                    stored = self._snapshot_store.put(stamped, snapshot_id=assigned_snapshot_id)
                    # Publish the provenance on the caller's result object as
                    # well.  The store retained a deep copy, so this
                    # convenience mutation cannot make its entry mutable.
                    snapshot.session_id = current.session_id
                    snapshot.generation = current.generation
                    snapshot.screen_revision = current.screen_revision
                    snapshot.snapshot_id = stored.snapshot_id
                    for source, stored_element in zip(
                        snapshot.elements,
                        stored.elements,
                        strict=True,
                    ):
                        source.element_ref = stored_element.element_ref
                        source.bounds_provenance = (
                            stored_element.bounds_provenance.model_copy(deep=True)
                            if stored_element.bounds_provenance is not None
                            else None
                        )
                for source, _stored_element in zip(snapshot.elements, stored.elements, strict=True):
                    if source.bounds_provenance is not None:
                        source.bounds_provenance.snapshot_id = stored.snapshot_id
                self._legacy_snapshot_input = snapshot
                self._snapshot_id = stored.snapshot_id
                self._snapshot = stored
                self._current_snapshot_revision_managed = expected_context is not None
                return cast(str, stored.snapshot_id)

        return cast(str, _commit(active_operation, store, "snapshot_commit"))

    def get_snapshot(
        self,
        snapshot_id: str,
        *,
        operation: OperationContext | None = None,
    ) -> ScreenSnapshot:
        """Return one immutable identified observation.

        Older revisions remain readable.  A snapshot from another connected
        session is reported as stale even when it has not reached store
        capacity, preventing cross-device reuse of an opaque ID.
        """

        active_operation = _effective_operation(operation)
        _checkpoint(active_operation, "snapshot_read")
        with self._state_lock:
            snapshot = self._snapshot_store.get(
                snapshot_id,
                clone=not (
                    self._legacy_snapshot_input is not None
                    and self._snapshot_id == snapshot_id
                    and self._session_id is None
                ),
            )
            if snapshot.session_id is not None and snapshot.session_id != self._session_id:
                raise MobileUseError(
                    ErrorCode.SNAPSHOT_STALE,
                    f"Android snapshot {snapshot_id!r} belongs to an earlier Android session.",
                    "Call android_snapshot to capture the current screen and use its new "
                    "snapshot_id.",
                    data={
                        "snapshot_id": snapshot_id,
                        "snapshot_session_id": snapshot.session_id,
                        "current_session_id": self._session_id,
                    },
                )
        _checkpoint(active_operation, "snapshot_read")
        return snapshot

    def clear_snapshot(self, *, operation: OperationContext | None = None) -> None:
        """Advance the screen revision and clear only the current pointer.

        Calls from the operation gateway represent a known screen-changing
        operation.  Direct legacy callers retain the old explicit-clear
        behavior and remove only the current entry.
        """

        active_operation = _effective_operation(operation)
        if active_operation is None:
            with self._state_lock:
                current_id = self._snapshot_id
                self._clear_snapshot_unchecked()
                if current_id is not None:
                    self._snapshot_store.forget(current_id)
                self._screen_revision += 1
            return
        # Legacy direct callers can still store an unbound snapshot while no
        # device session exists.  Such entries never had revision provenance,
        # so retain the old invalidation behavior for them; session-bound
        # public observations remain pageable after a mutation.
        with self._state_lock:
            current_id = self._snapshot_id
            current_snapshot = self._snapshot
            if (
                current_id is not None
                and current_snapshot is not None
                and current_snapshot.session_id is None
                and not self._current_snapshot_revision_managed
            ):
                self._snapshot_store.forget(current_id)
        self.advance_screen_revision(operation=active_operation)

    def _clear_snapshot_unchecked(self) -> None:
        with self._state_lock:
            self._snapshot_id = None
            self._snapshot = None
            self._current_snapshot_revision_managed = False

    def advance_screen_revision(
        self,
        reason: str | None = None,
        *,
        operation: OperationContext | None = None,
    ) -> int:
        """Advance revision before dispatching a known screen mutation."""

        del reason  # Reserved for future local diagnostics; never log content.
        active_operation = _effective_operation(operation)

        def advance() -> int:
            with self._state_lock:
                current_id = self._snapshot_id
                current_snapshot = self._snapshot
                if (
                    current_id is not None
                    and current_snapshot is not None
                    and current_snapshot.session_id is None
                    and not self._current_snapshot_revision_managed
                ):
                    self._snapshot_store.forget(current_id)
                self._screen_revision += 1
                self._clear_snapshot_unchecked()
                return self._screen_revision

        return cast(int, _commit(active_operation, advance, "screen_revision"))

    # Descriptive aliases used by embedders and tests.
    advance_revision = advance_screen_revision
    mark_screen_changed = advance_screen_revision

    @staticmethod
    def _element_identity(element: UIElement) -> tuple[object, ...]:
        """Return stable node identity fields while ignoring volatile state."""

        return (
            element.text,
            element.content_description,
            element.resource_id,
            element.class_name,
            element.package,
            element.clickable,
            element.focusable,
            element.scrollable,
        )

    def _find_current_element(
        self,
        source_snapshot: ScreenSnapshot,
        source_element: UIElement,
        current_snapshot: ScreenSnapshot,
    ) -> UIElement | None:
        """Find the same observed node in a fresh complete hierarchy."""

        source_candidates = [
            index
            for index, candidate in enumerate(source_snapshot.elements)
            if self._element_identity(candidate) == self._element_identity(source_element)
        ]
        source_position = next(
            (
                index
                for index, candidate in enumerate(source_snapshot.elements)
                if candidate.element_ref == source_element.element_ref
            ),
            None,
        )
        ordinal = (
            source_candidates.index(source_position)
            if source_position is not None and source_position in source_candidates
            else 0
        )
        if source_element.node_index is not None:
            indexed = [
                candidate
                for candidate in current_snapshot.elements
                if candidate.node_index == source_element.node_index
                and self._element_identity(candidate) == self._element_identity(source_element)
            ]
            if indexed:
                return indexed[0]
        current_candidates = [
            candidate
            for candidate in current_snapshot.elements
            if self._element_identity(candidate) == self._element_identity(source_element)
        ]
        if ordinal < len(current_candidates):
            return current_candidates[ordinal]
        return None

    def _raise_stale_target(
        self,
        *,
        code: ErrorCode,
        message: str,
        suggestion: str,
        expected: SnapshotContext | None = None,
        actual: SnapshotContext | None = None,
        data: dict[str, object] | None = None,
        selector: str = "element_ref_or_bounds",
    ) -> Never:
        details = dict(data or {})
        details.setdefault(
            "attempts",
            [
                {
                    "selector": selector,
                    "matched": False,
                    "error": message,
                    "match_count": 0,
                }
            ],
        )
        details.setdefault("matched_selector", None)
        if expected is not None:
            details.update(
                {
                    "expected_session_id": expected.session_id,
                    "expected_generation": expected.generation,
                    "expected_screen_revision": expected.screen_revision,
                }
            )
        if actual is not None:
            details.update(
                {
                    "current_session_id": actual.session_id,
                    "current_generation": actual.generation,
                    "current_screen_revision": actual.screen_revision,
                }
            )
        raise MobileUseError(code, message, suggestion, data=details)

    @staticmethod
    def _annotate_target_error(
        error: MobileUseError,
        *,
        selector: str,
    ) -> MobileUseError:
        """Add the common target ledger to snapshot lookup failures."""

        details = dict(error.data)
        details.setdefault(
            "attempts",
            [
                {
                    "selector": selector,
                    "matched": False,
                    "error": error.message,
                    "match_count": 0,
                }
            ],
        )
        details.setdefault("matched_selector", None)
        return MobileUseError(
            error.code,
            error.message,
            error.suggestion,
            data=details,
            retryable_override=error.retryable,
        )

    @staticmethod
    async def _capture_complete_snapshot(controller: Any, max_text_length: int) -> ScreenSnapshot:
        """Capture an action-time hierarchy without response pagination."""

        reader = controller.snapshot
        try:
            result = reader(
                interactive_only=False,
                max_elements=None,
                max_text_length=max_text_length,
            )
        except TypeError as error:
            # Small injected fakes often expose a no-argument snapshot method.
            # Only fall back for signature mismatches, preserving adapter
            # TypeErrors as real failures.
            if "unexpected keyword" not in str(error) and "positional argument" not in str(error):
                raise
            result = reader()
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ScreenSnapshot):
            raise TypeError("Android controller did not return a screen snapshot")
        return result

    # Keep the complete-read seam public for injected controllers and server
    # orchestration without exposing the private implementation name.
    capture_complete_snapshot = _capture_complete_snapshot

    async def resolve_target(
        self,
        target: Target,
        *,
        operation: OperationContext | None = None,
        controller: Any | None = None,
    ) -> tuple[ScreenSnapshot, ResolvedTarget]:
        """Validate a target against the current session before dispatch.

        Opaque references and copied bounds are capabilities, not hints: their
        provenance must still match the active session generation and screen
        revision, and a fresh complete hierarchy must contain the referenced
        enabled node at the same bounds.
        """

        active_operation = _effective_operation(operation)
        _checkpoint(active_operation, "target_resolve")
        with self._state_lock:
            current_context = SnapshotContext(
                session_id=self._session_id,
                generation=self._generation,
                screen_revision=self._screen_revision,
            )
        if controller is None:
            controller = self.require_controller(operation=active_operation)

        source_snapshot: ScreenSnapshot | None = None
        source_element: UIElement | None = None
        ref_snapshot_id: str | None = None
        if target.element_ref is not None:
            snapshot_id, source_snapshot, source_element = self._snapshot_store.find_element_ref(
                target.element_ref
            )
            ref_snapshot_id = snapshot_id
            if target.snapshot_id is not None and target.snapshot_id != snapshot_id:
                raise MobileUseError(
                    ErrorCode.SELECTOR_CONFLICT,
                    "The element_ref and snapshot_id identify different observations.",
                    "Copy both values from the same android_snapshot result.",
                    data={
                        "element_ref": target.element_ref,
                        "snapshot_id": target.snapshot_id,
                        "attempts": [
                            {
                                "selector": "element_ref",
                                "matched": True,
                                "error": None,
                                "match_count": 1,
                            },
                            {
                                "selector": "snapshot_id",
                                "matched": False,
                                "error": "Observation identity conflict",
                                "match_count": 0,
                            },
                        ],
                        "matched_selector": None,
                    },
                )
            provenance = source_element.bounds_provenance
            if provenance is None:
                self._raise_stale_target(
                    code=ErrorCode.ELEMENT_REF_STALE,
                    message="The Android element reference has no capture provenance.",
                    suggestion="Call android_snapshot and use an element_ref from that result.",
                    data={"element_ref": target.element_ref},
                )
            if provenance.session_id is None or provenance.generation is None:
                self._raise_stale_target(
                    code=ErrorCode.ELEMENT_REF_STALE,
                    message="The Android element reference is not bound to a live session.",
                    suggestion=(
                        "Call android_snapshot after android_connect and use its element_ref."
                    ),
                    data={"element_ref": target.element_ref},
                )
            expected = SnapshotContext(
                session_id=provenance.session_id,
                generation=provenance.generation,
                screen_revision=provenance.screen_revision,
            )
            if expected != current_context:
                self._raise_stale_target(
                    code=ErrorCode.ELEMENT_REF_STALE,
                    message="The Android element reference belongs to an older screen revision.",
                    suggestion=(
                        "Call android_snapshot and use an element_ref from the current screen."
                    ),
                    expected=expected,
                    actual=current_context,
                    data={"element_ref": target.element_ref},
                )
            if not source_element.enabled:
                raise MobileUseError(
                    ErrorCode.ELEMENT_DISABLED,
                    "The referenced Android element was disabled when observed.",
                    "Choose an enabled element from a fresh android_snapshot.",
                    data={
                        "element_ref": target.element_ref,
                        "attempts": [
                            {
                                "selector": "element_ref",
                                "matched": False,
                                "error": "The referenced Android element was disabled.",
                                "match_count": 1,
                            }
                        ],
                        "matched_selector": None,
                    },
                )

        if target.bounds_provenance is not None:
            expected = SnapshotContext(
                session_id=target.bounds_provenance.session_id,
                generation=target.bounds_provenance.generation,
                screen_revision=target.bounds_provenance.screen_revision,
            )
            if expected != current_context:
                self._raise_stale_target(
                    code=ErrorCode.ELEMENT_REF_STALE,
                    message="The Android bounds belong to an older screen revision.",
                    suggestion="Call android_snapshot and use bounds from the current screen.",
                    expected=expected,
                    actual=current_context,
                )
            provenance_snapshot_id = target.bounds_provenance.snapshot_id
            if (
                provenance_snapshot_id is not None
                and target.snapshot_id is not None
                and provenance_snapshot_id != target.snapshot_id
            ):
                raise MobileUseError(
                    ErrorCode.SELECTOR_CONFLICT,
                    "The snapshot_id and bounds provenance identify different observations.",
                    "Copy both values from the same android_snapshot result.",
                    data={
                        "snapshot_id": target.snapshot_id,
                        "attempts": [
                            {
                                "selector": "snapshot_id",
                                "matched": False,
                                "error": "Observation identity conflict",
                                "match_count": 0,
                            },
                            {
                                "selector": "bounds_provenance",
                                "matched": False,
                                "error": "Observation identity conflict",
                                "match_count": 0,
                            },
                        ],
                        "matched_selector": None,
                    },
                )
            if (
                provenance_snapshot_id is not None
                and ref_snapshot_id is not None
                and provenance_snapshot_id != ref_snapshot_id
            ):
                raise MobileUseError(
                    ErrorCode.SELECTOR_CONFLICT,
                    "The element_ref and bounds provenance identify different observations.",
                    "Copy both values from the same android_snapshot result.",
                    data={
                        "element_ref": target.element_ref,
                        "snapshot_id": target.snapshot_id,
                        "attempts": [
                            {
                                "selector": "element_ref",
                                "matched": True,
                                "error": None,
                                "match_count": 1,
                            },
                            {
                                "selector": "bounds_provenance",
                                "matched": False,
                                "error": "Observation identity conflict",
                                "match_count": 0,
                            },
                        ],
                        "matched_selector": None,
                    },
                )
            if provenance_snapshot_id is not None or target.snapshot_id is not None:
                source_id = provenance_snapshot_id or target.snapshot_id
                assert source_id is not None
                try:
                    source_snapshot = self.get_snapshot(source_id)
                except MobileUseError as error:
                    raise self._annotate_target_error(
                        error,
                        selector="bounds_provenance",
                    ) from error
        elif target.snapshot_id is not None:
            try:
                source_snapshot = self.get_snapshot(target.snapshot_id)
            except MobileUseError as error:
                raise self._annotate_target_error(error, selector="snapshot_id") from error
            expected = SnapshotContext(
                session_id=source_snapshot.session_id,
                generation=source_snapshot.generation,
                screen_revision=source_snapshot.screen_revision,
            )
            if expected != current_context:
                self._raise_stale_target(
                    code=ErrorCode.ELEMENT_REF_STALE,
                    message="The Android bounds belong to an older screen revision.",
                    suggestion="Call android_snapshot and use bounds from the current screen.",
                    expected=expected,
                    actual=current_context,
                    data={"snapshot_id": target.snapshot_id},
                )

        if (
            target.bounds is not None
            and current_context.session_id is not None
            and target.bounds_provenance is None
        ):
            raise MobileUseError(
                ErrorCode.INVALID_TARGET,
                "Coordinate targets on a connected Android session require bounds provenance.",
                "Copy bounds and bounds_provenance from the current android_snapshot, or use a "
                "resource-id/text selector.",
                data={
                    "attempts": [
                        {
                            "selector": "bounds",
                            "matched": False,
                            "error": "Missing bounds provenance.",
                            "match_count": 0,
                        }
                    ],
                    "matched_selector": None,
                },
            )

        current_snapshot = await self._capture_complete_snapshot(
            controller,
            min(2_000, self.config.snapshot_max_text_length),
        )
        _checkpoint(active_operation, "target_resolve")
        with self._state_lock:
            latest_context = SnapshotContext(
                session_id=self._session_id,
                generation=self._generation,
                screen_revision=self._screen_revision,
            )
        if latest_context != current_context:
            self._raise_stale_target(
                code=ErrorCode.ELEMENT_REF_STALE,
                message="The Android screen changed while the target was being resolved.",
                suggestion="Call android_snapshot and use a target from the current screen.",
                expected=current_context,
                actual=latest_context,
                data={"element_ref": target.element_ref},
            )

        if target.bounds is not None and not target.bounds.is_within(
            current_snapshot.width,
            current_snapshot.height,
        ):
            raise MobileUseError(
                ErrorCode.TARGET_OFF_SCREEN,
                "The Android target bounds are off-screen in the current observation.",
                "Call android_snapshot and choose bounds inside the current screen.",
                data={
                    "attempts": [
                        {
                            "selector": "bounds",
                            "matched": False,
                            "error": "Center is outside the current screen.",
                            "match_count": 0,
                        }
                    ],
                    "matched_selector": None,
                },
            )

        if source_snapshot is not None and source_snapshot.session_id != current_context.session_id:
            self._raise_stale_target(
                code=ErrorCode.ELEMENT_REF_STALE,
                message="The Android target belongs to another connected session.",
                suggestion="Call android_snapshot and use a target from this session.",
            )

        if target.bounds is not None and source_snapshot is not None and source_element is None:
            source_bounds_element = next(
                (
                    element
                    for element in source_snapshot.elements
                    if element.bounds == target.bounds
                ),
                None,
            )
            if source_bounds_element is not None:
                if not source_bounds_element.enabled:
                    raise MobileUseError(
                        ErrorCode.ELEMENT_DISABLED,
                        "The Android element supplying these bounds is disabled.",
                        "Choose an enabled element from a fresh android_snapshot.",
                        data={
                            "snapshot_id": source_snapshot.snapshot_id,
                            "attempts": [
                                {
                                    "selector": "bounds",
                                    "matched": False,
                                    "error": (
                                        "The Android element supplying these bounds is disabled."
                                    ),
                                    "match_count": 1,
                                }
                            ],
                            "matched_selector": None,
                        },
                    )
                current_bounds_element = self._find_current_element(
                    source_snapshot,
                    source_bounds_element,
                    current_snapshot,
                )
                if current_bounds_element is None:
                    raise MobileUseError(
                        ErrorCode.ELEMENT_REMOVED,
                        "The Android element supplying these bounds is no longer present.",
                        "Call android_snapshot and use bounds from the current screen.",
                        data={
                            "snapshot_id": source_snapshot.snapshot_id,
                            "attempts": [
                                {
                                    "selector": "bounds",
                                    "matched": False,
                                    "error": (
                                        "The Android element supplying these bounds is no longer "
                                        "present."
                                    ),
                                    "match_count": 1,
                                }
                            ],
                            "matched_selector": None,
                        },
                    )
                if current_bounds_element.bounds != source_bounds_element.bounds:
                    raise MobileUseError(
                        ErrorCode.ELEMENT_REF_STALE,
                        "The Android element supplying these bounds moved since it was observed.",
                        "Call android_snapshot and use bounds from the current screen.",
                        data={
                            "snapshot_id": source_snapshot.snapshot_id,
                            "attempts": [
                                {
                                    "selector": "bounds",
                                    "matched": False,
                                    "error": (
                                        "The Android element supplying these bounds moved since it "
                                        "was observed."
                                    ),
                                    "match_count": 1,
                                }
                            ],
                            "matched_selector": None,
                        },
                    )
                if not current_bounds_element.enabled:
                    raise MobileUseError(
                        ErrorCode.ELEMENT_DISABLED,
                        "The Android element supplying these bounds is disabled.",
                        "Choose an enabled element from a fresh android_snapshot.",
                        data={
                            "snapshot_id": source_snapshot.snapshot_id,
                            "attempts": [
                                {
                                    "selector": "bounds",
                                    "matched": False,
                                    "error": (
                                        "The Android element supplying these bounds is disabled."
                                    ),
                                    "match_count": 1,
                                }
                            ],
                            "matched_selector": None,
                        },
                    )

        if source_element is not None:
            current_element = self._find_current_element(
                source_snapshot or current_snapshot,
                source_element,
                current_snapshot,
            )
            if current_element is None:
                raise MobileUseError(
                    ErrorCode.ELEMENT_REMOVED,
                    "The referenced Android element is no longer present on screen.",
                    "Call android_snapshot and use an element_ref from the current screen.",
                    data={
                        "element_ref": target.element_ref,
                        "attempts": [
                            {
                                "selector": "element_ref",
                                "matched": False,
                                "error": (
                                    "The referenced Android element is no longer present on screen."
                                ),
                                "match_count": 0,
                            }
                        ],
                        "matched_selector": None,
                    },
                )
            # Give the resolver an internal capability marker while keeping
            # the public fresh hierarchy free of stale reference values.
            current_elements = [
                element.model_copy(deep=True) for element in current_snapshot.elements
            ]
            current_index = next(
                index
                for index, element in enumerate(current_snapshot.elements)
                if element is current_element
            )
            current_elements[current_index].element_ref = target.element_ref
            if current_element.bounds != source_element.bounds:
                raise MobileUseError(
                    ErrorCode.ELEMENT_REF_STALE,
                    "The referenced Android element moved since it was observed.",
                    "Call android_snapshot and use an element_ref from the current screen.",
                    data={
                        "element_ref": target.element_ref,
                        "attempts": [
                            {
                                "selector": "element_ref",
                                "matched": False,
                                "error": (
                                    "The referenced Android element moved since it was observed."
                                ),
                                "match_count": 1,
                            }
                        ],
                        "matched_selector": None,
                    },
                )
            if target.bounds is not None and target.bounds != source_element.bounds:
                raise MobileUseError(
                    ErrorCode.SELECTOR_CONFLICT,
                    "The supplied bounds and element_ref identify different UI elements.",
                    "Use bounds copied from the same element observation.",
                    data={
                        "attempts": [
                            {
                                "selector": "element_ref",
                                "matched": True,
                                "error": None,
                                "match_count": 1,
                            },
                            {
                                "selector": "bounds",
                                "matched": True,
                                "error": None,
                                "match_count": 1,
                            },
                        ],
                        "matched_selector": None,
                    },
                )
            resolved = resolve_target(
                target,
                current_elements,
                screen_width=current_snapshot.width,
                screen_height=current_snapshot.height,
            )
        else:
            resolved = resolve_target(
                target,
                current_snapshot.elements,
                screen_width=current_snapshot.width,
                screen_height=current_snapshot.height,
            )
        return current_snapshot, resolved

    # Compatibility aliases used by integrations that call the seam by its
    # validation-oriented name.
    validate_target = resolve_target
    prepare_target = resolve_target

    def require_controller(
        self,
        *,
        operation_id: str | None = None,
        operation: OperationContext | None = None,
    ) -> AndroidController:
        """Return the active controller after probing the selected serial."""

        active_operation = _effective_operation(operation)
        effective_operation_id = operation_id or (
            active_operation.operation_id if active_operation is not None else _new_operation_id()
        )
        _checkpoint(active_operation)
        with self._lifecycle_lock:
            with self._state_lock:
                if self._controller is None or self._device is None:
                    raise MobileUseError(
                        ErrorCode.NOT_CONNECTED,
                        "No Android device is connected to the MCP session.",
                        "Call android_connect before using device tools.",
                    )
                selected_device = self._device
                controller = self._controller
                self._active_operation_id = effective_operation_id
            try:
                _checkpoint(active_operation)
                current = self.registry.select(selected_device.serial)
                _checkpoint(active_operation)
            except MobileUseError as error:
                if error.code in {
                    ErrorCode.DEVICE_NOT_FOUND,
                    ErrorCode.DEVICE_OFFLINE,
                    ErrorCode.DEVICE_UNAUTHORIZED,
                }:
                    disconnected = MobileUseError(
                        ErrorCode.DEVICE_DISCONNECTED,
                        f"Android device {selected_device.serial!r} is no longer connected "
                        "and ready.",
                        "Reconnect the device, verify `adb devices -l`, then call "
                        "android_connect again.",
                    )
                    if active_operation is None or active_operation.active:
                        _commit(
                            active_operation,
                            lambda: self._mark_degraded(disconnected),
                            "error_commit",
                        )
                    raise disconnected from error
                if active_operation is None or active_operation.active:
                    _commit(
                        active_operation,
                        lambda error=error: self._mark_degraded(error),
                        "error_commit",
                    )
                raise
            else:
                _commit(
                    active_operation,
                    lambda: self._mark_ready(current),
                    "session_commit",
                )
                _checkpoint(active_operation)
                return controller
            finally:
                if active_operation is None:
                    with self._state_lock:
                        if self._active_operation_id == effective_operation_id:
                            self._active_operation_id = None

    def _mark_degraded(self, error: MobileUseError) -> None:
        with self._state_lock:
            self._state = SessionLifecycle.DEGRADED
            self._last_error = _lifecycle_error(error)

    def _mark_ready(self, device: DeviceInfo) -> None:
        with self._state_lock:
            self._device = device
            self._state = SessionLifecycle.READY
            self._last_error = None

    def status(self, operation_id: str | None = None) -> DeviceStatusResult:
        """Return a safe typed status snapshot without probing the device."""

        op_id = operation_id or _new_operation_id()
        with self._state_lock:
            state = self.state
            device = self._device
            session_id = self._session_id
            generation = self._generation
            connected = (
                state.value == SessionLifecycle.READY.value
                and device is not None
                and (self._controller is not None or self._state == SessionLifecycle.DISCONNECTED)
            )
            if state == SessionLifecycle.DISCONNECTED:
                message = "No Android device is connected."
            elif state == SessionLifecycle.CONNECTING:
                message = "Connecting to an Android device."
            elif state == SessionLifecycle.READY and device is not None:
                message = f"Connected to Android device {device.serial}."
            elif state == SessionLifecycle.DEGRADED:
                message = "The Android session is degraded and the device is not ready."
            elif state == SessionLifecycle.CLOSING:
                message = "Closing the Android device session."
            else:
                message = "The Android device session failed to initialize."
            last_error = self._last_error
            return DeviceStatusResult(
                success=True,
                operation_id=op_id,
                message=message,
                state=state,
                session_id=session_id,
                generation=generation,
                device=device,
                connected=connected,
                active_operation_id=self._active_operation_id,
                screen_revision=self._screen_revision,
                current_snapshot_id=self._snapshot_id,
                snapshot_store=self._snapshot_store.stats(),
                last_error=last_error,
                error_code=last_error.code if last_error else None,
                category=last_error.category if last_error else None,
                retryable=last_error.retryable if last_error else None,
                suggestion=last_error.suggestion if last_error else None,
                details=last_error.details if last_error else {},
                policy=self.policy_summary(),
                package_policy=self.policy_summary(),
            )

    def get_status(self, operation_id: str | None = None) -> DeviceStatusResult:
        """Named alias for callers that prefer an explicit read-method name."""

        return self.status(operation_id)

    def disconnect(
        self,
        *,
        operation_id: str | None = None,
        operation: OperationContext | None = None,
    ) -> DeviceDisconnectResult:
        """Close the current session; repeated calls are safe and successful."""

        active_operation = _effective_operation(operation)
        effective_operation_id = operation_id or (
            active_operation.operation_id if active_operation is not None else _new_operation_id()
        )
        _checkpoint(active_operation)
        with self._lifecycle_lock:
            with self._state_lock:
                closed_session_id, closed_generation, controller = _commit(
                    active_operation,
                    lambda: self._prepare_disconnect_locked(effective_operation_id),
                    "session_commit",
                )
            cleanup_error: Exception | None = None
            if controller is not None:
                try:
                    _checkpoint(active_operation, "cleanup")
                    controller.disconnect()
                except Exception as error:
                    cleanup_error = error

            if cleanup_error is not None:
                failure = MobileUseError(
                    ErrorCode.OPERATION_FAILED,
                    "Failed to close the Android automation connection.",
                    "Retry android_disconnect; if it persists, restart the local MCP server.",
                )
                with self._state_lock:
                    result = _commit(
                        active_operation,
                        lambda: self._finish_disconnect_failure(
                            effective_operation_id,
                            closed_session_id,
                            closed_generation,
                            failure,
                        ),
                        "error_commit",
                    )
            else:
                with self._state_lock:
                    result = _commit(
                        active_operation,
                        lambda: self._finish_disconnect_success(
                            effective_operation_id,
                            closed_session_id,
                            closed_generation,
                        ),
                        "session_commit",
                    )
            if active_operation is None:
                with self._state_lock:
                    if self._active_operation_id == effective_operation_id:
                        self._active_operation_id = None
            return result

    def _prepare_disconnect_locked(
        self,
        operation_id: str,
    ) -> tuple[str | None, int | None, AndroidController | None]:
        closed_session_id = self._session_id
        closed_generation = self._generation
        self._active_operation_id = operation_id
        self._state = SessionLifecycle.CLOSING
        controller = self._controller
        self._controller = None
        self._device = None
        self._session_id = None
        self._generation = None
        self._clear_snapshot_unchecked()
        return closed_session_id, closed_generation, controller

    def _finish_disconnect_failure(
        self,
        operation_id: str,
        closed_session_id: str | None,
        closed_generation: int | None,
        failure: MobileUseError,
    ) -> DeviceDisconnectResult:
        self._state = SessionLifecycle.FAILED
        self._last_error = _lifecycle_error(failure)
        return DeviceDisconnectResult(
            success=False,
            operation_id=operation_id,
            message=failure.message,
            state=self._state,
            closed_session_id=closed_session_id,
            closed_generation=closed_generation,
            error_code=failure.code.value,
            category=failure.category.value,
            retryable=failure.retryable,
            suggestion=failure.suggestion,
            details={},
            error=self._last_error,
        )

    def _finish_disconnect_success(
        self,
        operation_id: str,
        closed_session_id: str | None,
        closed_generation: int | None,
    ) -> DeviceDisconnectResult:
        self._state = SessionLifecycle.DISCONNECTED
        self._last_error = None
        return DeviceDisconnectResult(
            success=True,
            operation_id=operation_id,
            message="Android device disconnected.",
            state=self._state,
            closed_session_id=closed_session_id,
            closed_generation=closed_generation,
        )

    async def close(self) -> DeviceDisconnectResult:
        """Awaitable shutdown hook backed by the explicit disconnect path."""

        operation_id = self.new_operation_id()
        with self._state_lock:
            self._disconnect_requested = True
        try:
            return await self.run_operation(
                "disconnect",
                self._disconnect_async,
                operation_id=operation_id,
                retry_safe=True,
                mutating=True,
            )
        finally:
            with self._state_lock:
                self._disconnect_requested = False

    async def _disconnect_async(self, operation: OperationContext) -> DeviceDisconnectResult:
        """Disconnect through the gateway while awaiting child-process reap."""

        active_operation = _effective_operation(operation)
        effective_operation_id = operation.operation_id
        _checkpoint(active_operation)
        with self._lifecycle_lock:
            with self._state_lock:
                closed_session_id, closed_generation, controller = _commit(
                    active_operation,
                    lambda: self._prepare_disconnect_locked(effective_operation_id),
                    "session_commit",
                )
            cleanup_error: Exception | None = None
            if controller is not None:
                cleanup_task = asyncio.create_task(
                    self._close_controller_async(controller),
                    name="android-controller-close",
                )
                try:
                    # The controller has already been detached from session
                    # state. Cleanup must continue even if the operation
                    # deadline/cancellation wins while the adapter closes.
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    # The caller may disappear while cleanup is in progress;
                    # shield one final await so a recording process cannot be
                    # left behind by MCP cancellation or EOF.
                    with suppress(Exception):
                        await asyncio.shield(cleanup_task)
                    raise
                except Exception as error:
                    cleanup_error = error

            with self._state_lock:
                if cleanup_error is not None:
                    failure = MobileUseError(
                        ErrorCode.OPERATION_FAILED,
                        "Failed to close the Android automation connection.",
                        "Retry android_disconnect; if it persists, restart the local MCP server.",
                    )
                    return cast(
                        DeviceDisconnectResult,
                        _commit(
                            active_operation,
                            lambda: self._finish_disconnect_failure(
                                effective_operation_id,
                                closed_session_id,
                                closed_generation,
                                failure,
                            ),
                            "error_commit",
                        ),
                    )
                return cast(
                    DeviceDisconnectResult,
                    _commit(
                        active_operation,
                        lambda: self._finish_disconnect_success(
                            effective_operation_id,
                            closed_session_id,
                            closed_generation,
                        ),
                        "session_commit",
                    ),
                )

    async def _close_controller_async(self, controller: AndroidController) -> None:
        """Use the awaitable close hook while keeping injected legacy fakes valid."""

        close_async = getattr(controller, "aclose", None)
        if callable(close_async):
            result = close_async()
            if inspect.isawaitable(result):
                await result
                return
        close_sync = getattr(controller, "disconnect", None)
        if callable(close_sync):
            result = await run_blocking(
                close_sync,
                timeout_seconds=self.config.subprocess_timeout_seconds,
            )
            if inspect.isawaitable(result):
                await result

    async def aclose(self) -> DeviceDisconnectResult:
        """Alias for callers that use the conventional async-close spelling."""

        return await self.close()

    async def shutdown(self) -> DeviceDisconnectResult:
        """Explicit alias used by embedding applications during server shutdown."""

        return await self.close()


# Preserve the old import for integrations that used the pre-lifecycle name.
SessionManager = DeviceSession


__all__ = ["ControllerFactory", "DeviceSession", "SessionManager"]
