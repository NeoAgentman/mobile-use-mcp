"""Android observation parsing and bounded immutable snapshot storage."""

import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from mobile_use_mcp.errors import ErrorCode, MobileUseError
from mobile_use_mcp.models import Bounds, ScreenSnapshot, UIElement

_BOUNDS_PATTERN = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")


def snapshot_size_bytes(snapshot: ScreenSnapshot) -> int:
    """Return a deterministic storage estimate for one observation.

    The image is counted at its native byte size.  The compact metadata and
    hierarchy are counted using their JSON representation, which gives the
    store a stable bound without serializing or retaining a second image.
    """

    metadata = snapshot.model_dump_json(exclude={"screenshot_png"}).encode("utf-8")
    return len(snapshot.screenshot_png) + len(metadata)


@dataclass(slots=True)
class _SnapshotEntry:
    snapshot: ScreenSnapshot
    stored_at: float
    size_bytes: int


class SnapshotStore:
    """Bounded store for immutable, identified Android observations.

    Entries are retained independently of the session's current-observation
    pointer.  Therefore a host can page an older snapshot after a screen
    mutation and still receive exactly the hierarchy captured at that point.
    Capacity eviction is FIFO (oldest insertion first), while expired entries
    are removed before applying count/byte pressure.  IDs removed for expiry
    and pressure are remembered long enough to provide distinct diagnostics.
    """

    def __init__(
        self,
        *,
        max_entries: int = 8,
        max_bytes: int = 64 * 1024 * 1024,
        ttl_seconds: float = 5 * 60,
        clock: Callable[[], float] = time.monotonic,
        max_count: int | None = None,
        max_size_bytes: int | None = None,
        retention_seconds: float | None = None,
    ) -> None:
        if max_count is not None:
            max_entries = max_count
        if max_size_bytes is not None:
            max_bytes = max_size_bytes
        if retention_seconds is not None:
            ttl_seconds = retention_seconds
        if max_entries < 1:
            raise ValueError("max_entries must be at least one")
        if max_bytes < 1:
            raise ValueError("max_bytes must be at least one")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, _SnapshotEntry] = OrderedDict()
        self._expired: OrderedDict[str, float] = OrderedDict()
        self._evicted: OrderedDict[str, float] = OrderedDict()
        self._element_refs: dict[str, str] = {}
        self._expired_element_refs: OrderedDict[str, float] = OrderedDict()
        self._evicted_element_refs: OrderedDict[str, float] = OrderedDict()
        self._total_bytes = 0
        self._lock = RLock()

    @property
    def count(self) -> int:
        with self._lock:
            self._prune_expired(self._clock())
            return len(self._entries)

    def __len__(self) -> int:
        return self.count

    def __contains__(self, snapshot_id: object) -> bool:
        if not isinstance(snapshot_id, str):
            return False
        with self._lock:
            self._prune_expired(self._clock())
            return snapshot_id in self._entries

    @property
    def entry_count(self) -> int:
        return self.count

    @property
    def max_count(self) -> int:
        """Compatibility alias for the configured entry bound."""

        return self.max_entries

    @property
    def total_bytes(self) -> int:
        with self._lock:
            self._prune_expired(self._clock())
            return self._total_bytes

    @property
    def max_size_bytes(self) -> int:
        """Compatibility alias for the configured byte bound."""

        return self.max_bytes

    @property
    def total_size_bytes(self) -> int:
        """Compatibility alias for the current stored-byte count."""

        return self.total_bytes

    @property
    def retention_seconds(self) -> float:
        """Compatibility alias for the configured TTL."""

        return self.ttl_seconds

    @property
    def snapshot_ids(self) -> tuple[str, ...]:
        with self._lock:
            self._prune_expired(self._clock())
            return tuple(self._entries)

    def stats(self) -> dict[str, int | float]:
        """Return bounded cache metrics suitable for status/diagnostics."""

        with self._lock:
            self._prune_expired(self._clock())
            return {
                "entry_count": len(self._entries),
                "total_bytes": self._total_bytes,
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
                "ttl_seconds": self.ttl_seconds,
            }

    def _remember(self, target: OrderedDict[str, float], snapshot_id: str, now: float) -> None:
        target[snapshot_id] = now
        # A diagnostic history should never become an unbounded side channel.
        while len(target) > max(16, self.max_entries * 4):
            target.popitem(last=False)

    @staticmethod
    def _snapshot_element_refs(snapshot: ScreenSnapshot) -> tuple[str, ...]:
        return tuple(
            element.element_ref for element in snapshot.elements if element.element_ref is not None
        )

    def _index_entry(self, snapshot_id: str, snapshot: ScreenSnapshot) -> None:
        for element_ref in self._snapshot_element_refs(snapshot):
            self._element_refs[element_ref] = snapshot_id
            self._expired_element_refs.pop(element_ref, None)
            self._evicted_element_refs.pop(element_ref, None)

    def _forget_entry_refs(
        self,
        snapshot_id: str,
        *,
        target: OrderedDict[str, float] | None = None,
        now: float | None = None,
    ) -> None:
        """Move refs for one removed entry to bounded stale diagnostics."""

        for element_ref, indexed_snapshot_id in tuple(self._element_refs.items()):
            if indexed_snapshot_id != snapshot_id:
                continue
            del self._element_refs[element_ref]
            if target is not None:
                self._remember(
                    target,
                    element_ref,
                    self._clock() if now is None else now,
                )

    def _prune_expired(self, now: float) -> None:
        if self.ttl_seconds < 0:  # Defensive guard for embedders mutating config.
            return
        expired_ids = [
            snapshot_id
            for snapshot_id, entry in self._entries.items()
            if now - entry.stored_at >= self.ttl_seconds
        ]
        for snapshot_id in expired_ids:
            entry = self._entries.pop(snapshot_id)
            self._total_bytes -= entry.size_bytes
            self._remember(self._expired, snapshot_id, entry.stored_at + self.ttl_seconds)
            self._forget_entry_refs(
                snapshot_id,
                target=self._expired_element_refs,
                now=entry.stored_at + self.ttl_seconds,
            )

    def _evict_for_pressure(self, now: float) -> None:
        while len(self._entries) > self.max_entries or self._total_bytes > self.max_bytes:
            snapshot_id, entry = self._entries.popitem(last=False)
            self._total_bytes -= entry.size_bytes
            self._remember(self._evicted, snapshot_id, now)
            self._forget_entry_refs(snapshot_id, target=self._evicted_element_refs, now=now)

    def put(
        self,
        snapshot: ScreenSnapshot,
        *,
        snapshot_id: str | None = None,
        clone: bool = True,
    ) -> ScreenSnapshot:
        """Store and return one observation with an opaque ID.

        ``clone`` exists only for old in-process callers that relied on object
        identity.  Public session captures use the default deep copy, so a
        caller cannot mutate a stored observation after insertion.
        """

        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            # IDs are generated by the store.  Reusing a model that already
            # carries an ID must create a new observation unless the caller
            # explicitly supplies an ID for an intentional replacement.
            resolved_id = snapshot_id or f"s-{uuid4().hex}"
            if resolved_id in self._entries:
                previous = self._entries.pop(resolved_id)
                self._total_bytes -= previous.size_bytes
                self._forget_entry_refs(resolved_id)
            stored = snapshot if not clone else snapshot.model_copy(deep=True)
            stored.snapshot_id = resolved_id
            size_bytes = snapshot_size_bytes(stored)
            if size_bytes > self.max_bytes:
                self._remember(self._evicted, resolved_id, now)
                raise MobileUseError(
                    ErrorCode.SNAPSHOT_TOO_LARGE,
                    "The Android snapshot exceeds the configured snapshot byte budget.",
                    "Request a smaller observation or increase the snapshot byte budget.",
                    data={
                        "snapshot_id": resolved_id,
                        "size_bytes": size_bytes,
                        "max_bytes": self.max_bytes,
                    },
                )
            self._expired.pop(resolved_id, None)
            self._evicted.pop(resolved_id, None)
            self._entries[resolved_id] = _SnapshotEntry(stored, now, size_bytes)
            self._index_entry(resolved_id, stored)
            self._total_bytes += size_bytes
            self._evict_for_pressure(now)
            return stored

    def replace_latest(self, snapshot: ScreenSnapshot, *, clone: bool = True) -> ScreenSnapshot:
        """Replace the current oldest/latest compatibility entry.

        This is intentionally narrow and used only by pre-revision callers
        that stored the exact same object twice.  Normal captures should call
        :meth:`put`, which retains all observations until pressure or TTL.
        """

        with self._lock:
            if self._entries:
                latest_id = next(reversed(self._entries))
                previous = self._entries.pop(latest_id)
                self._total_bytes -= previous.size_bytes
                self._forget_entry_refs(latest_id)
            return self.put(snapshot, clone=clone)

    def forget(self, snapshot_id: str, *, reason: str = "not_found") -> None:
        """Remove one entry, optionally retaining a stale diagnostic marker."""

        with self._lock:
            entry = self._entries.pop(snapshot_id, None)
            if entry is not None:
                self._total_bytes -= entry.size_bytes
                target = (
                    self._expired_element_refs
                    if reason == "expired"
                    else self._evicted_element_refs
                    if reason == "evicted"
                    else None
                )
                self._forget_entry_refs(snapshot_id, target=target)
            if reason == "expired":
                self._remember(self._expired, snapshot_id, self._clock())
            elif reason == "evicted":
                self._remember(self._evicted, snapshot_id, self._clock())

    def clear(self, *, reason: str = "not_found") -> None:
        """Drop all entries; retained IDs receive the requested diagnosis."""

        with self._lock:
            ids = tuple(self._entries)
            self._entries.clear()
            self._total_bytes = 0
            if reason in {"expired", "evicted"}:
                target = self._expired if reason == "expired" else self._evicted
                element_target = (
                    self._expired_element_refs
                    if reason == "expired"
                    else self._evicted_element_refs
                )
                now = self._clock()
                for snapshot_id in ids:
                    self._remember(target, snapshot_id, now)
                    self._forget_entry_refs(snapshot_id, target=element_target, now=now)
            else:
                for snapshot_id in ids:
                    self._forget_entry_refs(snapshot_id)

    def get(self, snapshot_id: str, *, clone: bool = True) -> ScreenSnapshot:
        """Return one identified observation or a specific expiry/stale error."""

        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            entry = self._entries.get(snapshot_id)
            if entry is not None:
                snapshot = entry.snapshot
                return snapshot.model_copy(deep=True) if clone else snapshot
            if snapshot_id in self._expired:
                age = max(0.0, now - self._expired[snapshot_id])
                raise MobileUseError(
                    ErrorCode.SNAPSHOT_EXPIRED,
                    f"Android snapshot {snapshot_id!r} has expired.",
                    "Call android_snapshot to capture the current screen and use its new "
                    "snapshot_id.",
                    data={
                        "snapshot_id": snapshot_id,
                        "ttl_seconds": self.ttl_seconds,
                        "expired_seconds_ago": round(age, 3),
                    },
                )
            if snapshot_id in self._evicted:
                raise MobileUseError(
                    ErrorCode.SNAPSHOT_STALE,
                    f"Android snapshot {snapshot_id!r} is no longer retained.",
                    "Call android_snapshot to capture the current screen and use its new "
                    "snapshot_id.",
                    data={"snapshot_id": snapshot_id, "reason": "capacity_eviction"},
                )
            raise MobileUseError(
                ErrorCode.SNAPSHOT_NOT_FOUND,
                f"Android snapshot {snapshot_id!r} is no longer available.",
                "Call android_snapshot to capture the current screen and use its new snapshot_id.",
                data={"snapshot_id": snapshot_id},
            )

    def find_element_ref(
        self,
        element_ref: str,
        *,
        clone: bool = True,
    ) -> tuple[str, ScreenSnapshot, UIElement]:
        """Resolve an opaque element ref without exposing selector internals.

        References are indexed when snapshots enter the store and removed with
        the corresponding bounded entry. This preserves a distinct expiry or
        capacity diagnostic even when the caller did not retain ``snapshot_id``.
        """

        now = self._clock()
        with self._lock:
            self._prune_expired(now)
            snapshot_id = self._element_refs.get(element_ref)
            if snapshot_id is not None:
                entry = self._entries.get(snapshot_id)
                if entry is not None:
                    for element in entry.snapshot.elements:
                        if element.element_ref == element_ref:
                            if clone:
                                snapshot = entry.snapshot.model_copy(deep=True)
                                matched = next(
                                    item
                                    for item in snapshot.elements
                                    if item.element_ref == element_ref
                                )
                            else:
                                snapshot = entry.snapshot
                                matched = element
                            return snapshot_id, snapshot, matched
            if element_ref in self._expired_element_refs:
                raise MobileUseError(
                    ErrorCode.ELEMENT_REF_EXPIRED,
                    "The Android element reference has expired with its snapshot.",
                    "Call android_snapshot and use an element_ref from the new observation.",
                    data={
                        "element_ref": element_ref,
                        "attempts": [
                            {
                                "selector": "element_ref",
                                "matched": False,
                                "error": "The Android element reference has expired.",
                                "match_count": 0,
                            }
                        ],
                        "matched_selector": None,
                    },
                )
            if element_ref in self._evicted_element_refs:
                raise MobileUseError(
                    ErrorCode.ELEMENT_REF_STALE,
                    "The Android element reference is no longer retained.",
                    "Call android_snapshot and use an element_ref from the new observation.",
                    data={
                        "element_ref": element_ref,
                        "reason": "capacity_eviction",
                        "attempts": [
                            {
                                "selector": "element_ref",
                                "matched": False,
                                "error": "The Android element reference was evicted.",
                                "match_count": 0,
                            }
                        ],
                        "matched_selector": None,
                    },
                )
            raise MobileUseError(
                ErrorCode.ELEMENT_REF_NOT_FOUND,
                "The Android element reference is unknown or invalid.",
                "Call android_snapshot and use an element_ref from the new observation.",
                data={
                    "element_ref": element_ref,
                    "attempts": [
                        {
                            "selector": "element_ref",
                            "matched": False,
                            "error": "No matching opaque element reference.",
                            "match_count": 0,
                        }
                    ],
                    "matched_selector": None,
                },
            )


def parse_bounds(value: str | None) -> Bounds | None:
    if not value:
        return None
    match = _BOUNDS_PATTERN.fullmatch(value)
    if match is None:
        return None
    left, top, right, bottom = (int(group) for group in match.groups())
    if right <= left or bottom <= top:
        return None
    return Bounds(x=left, y=top, width=right - left, height=bottom - top)


def _as_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.casefold() == "true"


def _clean(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        # Keep an explicit empty accessibility value. Interactive input
        # fields use it to prove that a clear command reached an empty state;
        # whitespace-only structural nodes are still filtered by
        # ``has_semantic_content``.
        return ""
    # Preserve leading/trailing spaces: they are observable text effects and
    # must not make a successful input verification look like a mismatch.
    return value[:max_length]


def parse_hierarchy(
    hierarchy_xml: str,
    *,
    interactive_only: bool = False,
    max_elements: int | None = 200,
    max_text_length: int = 500,
) -> list[UIElement]:
    """Flatten and compact a UIAutomator XML hierarchy."""

    if max_elements is not None and max_elements < 1:
        return []
    try:
        root = ET.fromstring(hierarchy_xml)
    except ET.ParseError as error:
        raise ValueError("UIAutomator returned invalid hierarchy XML") from error

    elements: list[UIElement] = []
    for node_index, node in enumerate(root.iter("node")):
        attributes = node.attrib
        element = UIElement(
            node_index=node_index,
            text=_clean(attributes.get("text"), max_text_length),
            content_description=_clean(attributes.get("content-desc"), max_text_length),
            resource_id=_clean(attributes.get("resource-id"), 512),
            class_name=_clean(attributes.get("class"), 512),
            package=_clean(attributes.get("package"), 512),
            bounds=parse_bounds(attributes.get("bounds")),
            clickable=_as_bool(attributes.get("clickable")),
            enabled=_as_bool(attributes.get("enabled"), default=True),
            focusable=_as_bool(attributes.get("focusable")),
            focused=_as_bool(attributes.get("focused")),
            scrollable=_as_bool(attributes.get("scrollable")),
            selected=_as_bool(attributes.get("selected")),
            checked=(
                _as_bool(attributes.get("checked"))
                if attributes.get("checkable") == "true"
                else None
            ),
            password=_as_bool(attributes.get("password")),
        )
        if element.bounds is None:
            continue
        if interactive_only and not element.is_interactive():
            continue
        if not element.has_semantic_content() and not element.is_interactive():
            continue
        elements.append(element)
        if max_elements is not None and len(elements) >= max_elements:
            break
    return elements
