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
from typing import Any, cast
from uuid import uuid4

from mobile_use_mcp.config import RuntimeConfig
from mobile_use_mcp.controller import AndroidController
from mobile_use_mcp.devices import DeviceRegistry
from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import (
    DeviceDisconnectResult,
    DeviceInfo,
    DeviceStatusResult,
    LifecycleError,
    ScreenSnapshot,
    SessionConnection,
    SessionLifecycle,
    SnapshotContext,
)
from mobile_use_mcp.operations import OperationContext, OperationGateway, current_operation
from mobile_use_mcp.processes import run_blocking
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
    ):
        self.config = config or RuntimeConfig.defaults()
        self.registry = registry or DeviceRegistry(config=self.config)
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

    def connect(
        self,
        serial: str | None = None,
        *,
        operation_id: str | None = None,
        operation: OperationContext | None = None,
    ) -> SessionConnection:
        """Connect one selected device and return a new session generation.

        A reconnect first detaches the previous controller. If any stage of
        the new attempt fails, both the newly-created controller and all prior
        session references are cleared, leaving no connected controller behind.
        """

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
                            lambda: self._commit_connection_locked(selected, controller),
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
    ) -> SessionConnection:
        self._generation_counter += 1
        self._generation = self._generation_counter
        self._session_id = _new_session_id()
        self._device = selected
        self._controller = controller
        self._screen_revision = 0
        self._state = SessionLifecycle.READY
        self._last_error = None
        self._clear_snapshot_unchecked()
        return SessionConnection(
            session_id=self._session_id,
            generation=self._generation,
            state=self._state,
            device=selected,
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
            expected_screen_revision
            if expected_screen_revision is not None
            else expected_revision
        )

        def store() -> str:
            with self._state_lock:
                current = SnapshotContext(
                    session_id=self._session_id,
                    generation=self._generation,
                    screen_revision=self._screen_revision,
                )
                context_mismatch = expected_context is not None and current != expected_context
                alias_mismatch = (
                    expected_context is None
                    and (
                        (expected_session_id is not None
                         and current.session_id != expected_session_id)
                        or (expected_generation is not None
                            and current.generation != expected_generation)
                        or (alias_revision is not None
                            and current.screen_revision != alias_revision)
                    )
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
                # The store owns the opaque identifier.  A caller-provided ID
                # must never allow an observation to overwrite another entry.
                stamped.snapshot_id = None

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
                    stored = self._snapshot_store.put(stamped, clone=False)
                else:
                    stored = self._snapshot_store.put(stamped)
                    # Publish the provenance on the caller's result object as
                    # well.  The store retained a deep copy, so this
                    # convenience mutation cannot make its entry mutable.
                    snapshot.session_id = current.session_id
                    snapshot.generation = current.generation
                    snapshot.screen_revision = current.screen_revision
                    snapshot.snapshot_id = stored.snapshot_id
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
                self._screen_revision += 1
                self._clear_snapshot_unchecked()
                return self._screen_revision

        return cast(int, _commit(active_operation, advance, "screen_revision"))

    # Descriptive aliases used by embedders and tests.
    advance_revision = advance_screen_revision
    mark_screen_changed = advance_screen_revision

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
        return await self.run_operation(
            "disconnect",
            self._disconnect_async,
            operation_id=operation_id,
            retry_safe=True,
            mutating=True,
        )

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
