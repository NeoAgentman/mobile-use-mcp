# mobile-use-mcp

`mobile-use-mcp` is a local stdio MCP server that lets coding agents such as Codex and
Claude Code observe and operate Android devices through ADB and uiautomator2.

The server contains no LLM and requires no model API key. The host agent interprets screenshots
and UI elements, plans actions, and calls MCP tools directly.

## Capabilities

- Discover, select, and disconnect Android physical devices and emulators
- Return screenshots as native MCP image content
- Encode original-resolution screenshots as PNG or quality-controlled JPEG
- Return compact or full UIAutomator accessibility hierarchies with query and pagination
- Tap or long press using opaque observed-element references, provenance-bound bounds, resource ID,
  or text fallbacks
- Swipe using validated screen pixels or resolution-independent percentage coordinates
- Type and clear text in the focused field
- Press a bounded set of Android keys
- List, launch, and terminate apps
- Diagnose App launch attempts and foreground blockers
- Open HTTP and HTTPS URLs
- Record bounded Android screen videos with automatic segment rollover
- Detect unauthorized, offline, disconnected, and ambiguous device states

## Non-goals and safety boundaries

The initial release intentionally excludes:

- iOS, IDB, and WebDriverAgent
- Embedded LLMs or model provider configuration
- Arbitrary ADB shell access
- APK installation or app uninstall
- Clearing app data, rebooting, shutting down, or factory reset
- Cloud devices, BrowserStack, Limrun, and telemetry

The server never automatically uninstalls software from the connected device. Screenshots and UI
text can contain sensitive information and are returned to the host agent, so use a trusted agent
and model environment.

The MCP initialize response includes service instructions that tell compatible host agents to use
these tools for natural-language requests involving a physical Android phone or mobile App, such
as opening an App, browsing content, reading comments, collecting visible data, tapping, swiping,
typing, screenshots, and screen recording. Users normally should not need to name `mobile-use` or
ADB explicitly, although tool selection ultimately remains under the host agent's control.

## Requirements

- macOS, Linux, or Windows with Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Android SDK platform-tools with `adb` on `PATH`
- A physical Android device with USB debugging enabled, or an Android emulator
- Optional: `ffmpeg` on `PATH` to merge recordings longer than one Android segment

Confirm ADB connectivity before starting:

```bash
adb devices -l
```

Expected online state:

```text
List of devices attached
ABC123 device product:... model:... transport_id:1
```

If the state is `unauthorized`, unlock the phone and accept the USB debugging prompt. If it is
`offline`, reconnect the device or restart the ADB server.

### Runtime configuration and diagnostics

The stdio process reads one frozen, validated runtime configuration at startup. Defaults keep the
normal local ADB server (`127.0.0.1:5037`), original snapshot limits, temporary recording files,
and warning-level logging. Every setting is optional; malformed values stop startup with a
value-free stderr diagnostic rather than falling back silently.

The supported `MOBILE_USE_*` variables are:

| Variable | Purpose |
|---|---|
| `MOBILE_USE_ADB_EXECUTABLE` (`MOBILE_USE_ADB_PATH`) | ADB executable path or command |
| `MOBILE_USE_ADB_HOST`, `MOBILE_USE_ADB_PORT` | ADB server endpoint |
| `MOBILE_USE_OPERATION_TIMEOUT_SECONDS`, `MOBILE_USE_QUEUE_TIMEOUT_SECONDS` | Operation and queue budgets |
| `MOBILE_USE_SNAPSHOT_MAX_ELEMENTS`, `MOBILE_USE_SNAPSHOT_MAX_TEXT_LENGTH`, `MOBILE_USE_SNAPSHOT_MAX_ENTRIES`, `MOBILE_USE_SNAPSHOT_MAX_BYTES`, `MOBILE_USE_SNAPSHOT_TTL_SECONDS`, `MOBILE_USE_PAYLOAD_MAX_BYTES` | Snapshot store and response budgets |
| `MOBILE_USE_SUBPROCESS_TIMEOUT_SECONDS`, `MOBILE_USE_SUBPROCESS_MAX_OUTPUT_BYTES`, `MOBILE_USE_SUBPROCESS_TERMINATE_TIMEOUT_SECONDS` | External process limits |
| `MOBILE_USE_ARTIFACT_ROOT`, `MOBILE_USE_ARTIFACT_RETENTION_SECONDS`, `MOBILE_USE_ARTIFACT_MAX_COUNT`, `MOBILE_USE_ARTIFACT_MAX_BYTES` | Recording artifact ownership and retention |
| `MOBILE_USE_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

Call `android_doctor` before a workflow to receive independent `ready`, `degraded`, or
`unavailable` checks for ADB, device authorization/state, uiautomator2, screenshots, hierarchy,
screen recording, and ffmpeg. If more than one device is online, pass the exact serial from
`android_list_devices`; the doctor never picks one implicitly.

All `android_` calls share one FIFO session operation gateway. The configured operation budget is
a total deadline that includes queue time; the queue budget is an additional upper bound for time
spent waiting behind an active call. A timeout reports its operation ID, stage (`queue` or
`execution`), elapsed time, and whether retrying is safe. Cancelling a queued call removes only
that call. Cancelling or timing out a running call invalidates its session/snapshot commit, while
the gateway keeps later calls in submission order. The gateway never retries a callback; callers
must not blindly retry a non-idempotent action after an uncertain device outcome.

## Installation

Clone the repository and install the locked environment:

```bash
git clone https://github.com/NeoAgentman/mobile-use-mcp.git
cd mobile-use-mcp
uv sync
```

The MCP server entry point is:

```bash
uv run mobile-use-mcp
```

It uses stdio and is intended to be started by an MCP client, not used as an interactive CLI.

### Release artifact checks

The package version is maintained once in `src/mobile_use_mcp/version.py`. Hatchling reads that
source for project metadata, and the runtime MCP initialize response reports the same value. To
repeat the local release checks after building:

```bash
uv build
uv run python scripts/release_audit.py
```

The audit checks the version source, release-tag convention when `--tag` is supplied, wheel and
sdist metadata, the `mobile-use-mcp` console entry point, `LICENSE`, `NOTICE`, and the hashed
upstream inventory in [`provenance.toml`](provenance.toml). The clean-environment stdio smoke
check used by CI can be run against a wheel-installed command with:

```bash
smoke_env="$(mktemp -d)"
python -m venv "$smoke_env"
"$smoke_env/bin/python" -m pip install dist/mobile_use_mcp-*.whl
"$smoke_env/bin/python" scripts/stdio_smoke.py \
  --command "$smoke_env/bin/mobile-use-mcp"
```

## Codex configuration

Add the local server with the Codex CLI, replacing the path with your clone location:

```bash
codex mcp add mobile-use -- \
  uv --directory /absolute/path/to/mobile-use-mcp run mobile-use-mcp
```

Equivalent `~/.codex/config.toml` configuration:

```toml
[mcp_servers.mobile-use]
command = "uv"
args = [
  "--directory",
  "/absolute/path/to/mobile-use-mcp",
  "run",
  "mobile-use-mcp",
]
```

Start a new Codex task after adding the server so the tool list is refreshed.

## Claude Code configuration

Add the same stdio server to Claude Code:

```bash
claude mcp add --scope user mobile-use -- \
  uv --directory /absolute/path/to/mobile-use-mcp run mobile-use-mcp
```

Use `--scope project` instead if the configuration should only apply to one project.

## Recommended agent workflow

1. Call `android_list_devices`.
2. Call `android_connect`, specifying a serial when more than one device is online.
3. Call `android_snapshot` to receive the screenshot and current UI elements.
4. Prefer an `element_ref` from the current snapshot. When copying bounds, copy its
   `bounds_provenance` as well.
5. Perform one action.
6. Call `android_snapshot` again to verify the resulting state.
7. If an action fails, use the returned selector attempts and refresh the snapshot.

`android_wait` is only a fixed delay. It does not wait for a condition or inspect whether an
element appeared. For synchronization, use one of the bounded condition waits below; every
successful condition wait returns the immutable final `snapshot_id` and its session/generation/
screen-revision context.

Example instruction for the host agent:

```text
Use the mobile-use MCP tools to open Android Settings, navigate to About phone,
and report the device model. Observe the screen again after every action.
```

## Tools

### Device lifecycle

| Tool | Purpose |
|---|---|
| `android_list_devices` | List online, offline, and unauthorized ADB devices |
| `android_doctor` | Diagnose ADB, device authorization, uiautomator2, screenshot, hierarchy, screen recording, and ffmpeg |
| `android_connect` | Select and initialize one device |
| `android_status` | Read current MCP session state |
| `android_disconnect` | Release the active session |

### Observation

| Tool | Purpose |
|---|---|
| `android_snapshot` | Screenshot plus compact or full UI elements and truncation metadata |
| `android_screenshot` | Screenshot plus basic metadata |
| `android_get_ui_elements` | Page and query elements from one cached snapshot |
| `android_get_foreground_app` | Current package and activity |
| `android_list_apps` | Filter third-party package names |

### Condition waits

| Tool | Purpose |
|---|---|
| `android_wait_for_text` | Poll complete hierarchy until text or content description appears |
| `android_wait_for_element` | Poll complete hierarchy until a `Target` is present |
| `android_wait_for_ui_change` | Poll until content differs from an explicit baseline snapshot or screen revision |

Condition waits use a total monotonic timeout (default `10` seconds) and bounded polling (default
`0.25` seconds, maximum `5` seconds). Queue time counts toward the timeout. Text matching is a
case-insensitive substring search across text and content descriptions; element waits accept the
same strict `Target` selectors as actions and do not tap or mutate the device. The element wait
reads the complete hierarchy on every poll, so nodes beyond the compact snapshot page are still
found.

UI-change waits require `baseline_snapshot_id` or `baseline_screen_revision`. A snapshot baseline
compares the original screenshot, dimensions, foreground app, and normalized hierarchy. A screen
revision baseline succeeds when the session revision changes; supplying both uses either explicit
change signal. A successful result includes the final immutable `snapshot_id`, `session_id`,
`generation`, `screen_revision`, and `captured_at`. Timeout and disconnect results are MCP errors
and include bounded `poll_count`, `elapsed_ms`, `last_safe_observation`, and error-stage details.

`android_snapshot` defaults to `detail_level="compact"`, returns at most 200 elements, and reports
`snapshot_id`, `session_id`, `generation`, `screen_revision`, `captured_at`, `total_elements`,
`returned_elements`, `truncated`, and `next_offset`; no truncation is silent. Snapshots are bounded
by entry count, total stored bytes, and TTL. Capacity eviction is deterministic and returns a
`SNAPSHOT_STALE` diagnostic; TTL expiry returns `SNAPSHOT_EXPIRED`. An identified snapshot remains
immutable and pageable after later screen mutations while it is retained. The default
`max_text_length=500` is applied separately to each node's text and content description, including
in full mode. Increase it up to 2000 when reading long-form content.

When `truncated=true`, first query or page the same snapshot with `android_get_ui_elements`; use
`detail_level="full"` only as an explicit large-output fallback. `interactive_only=true` keeps only
clickable, focusable, or scrollable nodes, so leave it false when reading static content such as
articles, product descriptions, prices, tables, or comments.

`android_snapshot` and `android_screenshot` support `image_format` and `image_quality`.
Screenshots always keep the original device resolution. The default is JPEG quality 60; use
`image_format="png"` when small text, tables, icons, or other visual details require the original
lossless image. Every JPEG response includes a `quality_notice` and machine-readable
`lossless_fallback` with the exact PNG retry arguments. Screenshot base64 is not duplicated in
structured JSON. Use `android_snapshot` when UI text or targets are needed; use
`android_screenshot` for visual-only inspection. The latter reads native pixels only: it does not
collect hierarchy or foreground-App state and does not create a pageable snapshot. Combined
snapshots continue with a safe warning when optional hierarchy or foreground-App collection fails.

Use `android_get_ui_elements` with the returned `snapshot_id` to page the exact same hierarchy.
Its default page size is 100. It supports `offset`, `limit`, a case-insensitive `query` across
text, content description, resource ID, class, and package, plus exact package and interactive-only
filters. Filters are applied before pagination; reset `offset` to zero when changing a filter:

```json
{
  "snapshot_id": "s-...",
  "query": "校招",
  "package": "com.taobao.idlefish",
  "offset": 0,
  "limit": 50
}
```

Snapshots are retained independently of the current-observation pointer. Actions and the fixed
`android_wait` delay advance the screen revision, but an identified snapshot remains stable for
paging until its configured TTL or capacity eviction. Reconnecting or disconnecting makes
observations from the previous session stale; an expired ID returns `SNAPSHOT_EXPIRED`, while
capacity eviction returns `SNAPSHOT_STALE`, and both instruct the agent to observe again.

### Actions

| Tool | Purpose |
|---|---|
| `android_tap` | Tap using an opaque ref, provenance-bound bounds, or selector fallbacks |
| `android_long_press` | Long press an opaque ref, provenance-bound bounds, or selector fallbacks |
| `android_swipe` | Swipe between validated pixel coordinates or normalized percentages |
| `android_type_text` | Optionally focus a target, then type text with command/effect/IME status |
| `android_clear_text` | Optionally focus a target, then bounded-clear text and verify emptiness |
| `android_press_key` | Press back/home/enter/delete/tab/menu/volume keys |
| `android_launch_app` | Launch a package and poll until foreground |
| `android_terminate_app` | Force-stop a package |
| `android_open_url` | Open an HTTP or HTTPS URL |
| `android_start_recording` | Start a bounded screen recording with automatic segment rollover |
| `android_stop_recording` | Stop, pull, and optionally merge recording segments |
| `android_wait` | Sleep for a bounded fixed delay and advance the screen revision |
| `android_wait_for_text` | Wait for text/content description in the complete hierarchy |
| `android_wait_for_element` | Wait for a `Target` in the complete hierarchy |
| `android_wait_for_ui_change` | Wait for change from an explicit snapshot or screen-revision baseline |

`android_launch_app` reports every launch attempt, polling count, and the last foreground App. A
permission controller, chooser, or other foreground blocker is reported separately from an App
that remains in an indeterminate loading state.

Recordings are limited to 5–1800 seconds. Device-side temporary files use randomized names and are
removed after transfer. Android's per-recording time limit is handled with 170-second segments. If
`ffmpeg` is available, multiple segments are losslessly concatenated; otherwise the tool returns
the segment paths and a warning. Returned paths are local to the MCP host and may contain sensitive
screen content.

## Target selection

Tap and long-press tools accept a `target` object:

```json
{
  "element_ref": "el-...",
  "snapshot_id": "s-...",
  "bounds": {"x": 20, "y": 100, "width": 280, "height": 80},
  "bounds_provenance": {
    "snapshot_id": "s-...",
    "session_id": "session-...",
    "generation": 3,
    "screen_revision": 7
  },
  "resource_id": "com.example:id/continue",
  "resource_id_index": 0,
  "text": "Continue",
  "text_index": 0
}
```

The server evaluates every supplied selector and records all attempts. If two semantic selectors
identify different nodes, it returns `SELECTOR_CONFLICT` and dispatches no device command. When
the target comes from an active session snapshot, `element_ref` is preferred and provides the
strongest identity check.

For a target without an opaque reference, the server tries selectors in this order:

1. Valid on-screen bounds
2. Resource ID and occurrence index
3. Exact case-insensitive text/content description and occurrence index

`Target` does not accept `content_description`, `class_name`, or `package` fields. To act on a
snapshot node whose accessible label is in `content_description`, pass that value through the
target's `text` field. `class_name` and `package` are available as query filters through
`android_get_ui_elements`, not as action selectors. Bounds use original device pixels and become
stale after the UI changes. A connected-session coordinate target must include the
`bounds_provenance` returned with the snapshot element; its session ID, generation, screen
revision, and (when present) snapshot ID are checked before ADB dispatch. References and
provenance from expired, evicted, prior-generation, removed, disabled, or off-screen elements are
rejected without issuing a tap or long-press command.

`android_swipe` accepts either all four existing pixel fields (`start_x`, `start_y`, `end_x`,
`end_y`) or one `percentage` object with four values in `[0, 1]` (the equivalent flat
`*_percent` fields, or `coordinate_mode="percentage"` with flat values, are also accepted).
Mixing modes or supplying a partial mode is rejected. The result always reports the actual native
pixels sent to Android, including the conversion for percentage mode.

Failure responses include every attempted selector and recommend taking a fresh snapshot.

## Error model

Expected operational failures return structured data instead of internal tracebacks:

```json
{
  "success": false,
  "error_code": "DEVICE_DISCONNECTED",
  "message": "Android device 'ABC123' is no longer connected and ready.",
  "suggestion": "Reconnect the device, verify `adb devices -l`, then call android_connect again.",
  "data": {}
}
```

Stable error codes include:

- `ADB_UNAVAILABLE`
- `DEVICE_NOT_FOUND`
- `DEVICE_UNAUTHORIZED`
- `DEVICE_OFFLINE`
- `DEVICE_DISCONNECTED`
- `MULTIPLE_DEVICES`
- `NOT_CONNECTED`
- `INVALID_TARGET`
- `ELEMENT_NOT_FOUND`
- `INVALID_COORDINATES`
- `OPERATION_FAILED`
- `TIMEOUT`
- `CANCELLED`
- `BUSY`
- `UNSUPPORTED`
- `SNAPSHOT_NOT_FOUND`
- `SNAPSHOT_EXPIRED`
- `SNAPSHOT_STALE`
- `ELEMENT_REF_NOT_FOUND`
- `ELEMENT_REF_EXPIRED`
- `ELEMENT_REF_STALE`
- `ELEMENT_REMOVED`
- `ELEMENT_DISABLED`
- `TARGET_OFF_SCREEN`
- `SELECTOR_CONFLICT`

## Development and verification

Install development dependencies:

```bash
uv sync --dev
```

Run all non-device checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Inspect the live stdio MCP schema:

```bash
npx -y @modelcontextprotocol/inspector --cli \
  uv --directory "$PWD" run mobile-use-mcp \
  --method tools/list
```

Tests marked `android` require a connected device or emulator:

```bash
MOBILE_USE_ANDROID_SERIAL=ABC123 uv run pytest -m android
```

Device tests are excluded from the default test run and require an explicit serial so the test
suite never selects and operates a connected phone accidentally.

See [`COMPATIBILITY.md`](COMPATIBILITY.md) for current Codex, Claude Code, MCP client, and real
device verification results.

## Known limitations

- Custom-rendered Canvas/game interfaces may not expose useful accessibility elements; coordinate
  actions can still be selected from the screenshot.
- Screen recording availability and supported resolution depend on the Android device's built-in
  `screenrecord` implementation.
- UI elements are snapshots, not stable DOM nodes. Observe again after navigation, animation, or
  scrolling. Identified snapshots can be paged while retained, but `element_ref` and copied bounds
  are only valid for their originating session generation and screen revision.
- `detail_level="full"` can produce a large tool result. Prefer pagination and query first.
- `max_text_length` is a per-node limit, not a total response budget; full mode does not disable it.
- `interactive_only=true` intentionally hides most static text nodes and should not be used for
  long-form reading or content extraction.
- Screenshots always use the phone's original resolution. JPEG is lossy by default; retry with PNG
  when the returned quality notice indicates that visual detail may be insufficient.
- `android_type_text` and `android_clear_text` accept an optional target. Without one, they operate
  on the currently focused field.
- Text operations keep the payload in the device call and in-memory verification only. Results
  never echo plaintext and report `command_status`, `effect_status`, `focus_status`, and
  `restoration_status` independently. `effect_status="unverified"` means accessibility data was
  insufficient; observe the field before deciding whether another action is needed.
- If a text command fails after it may have reached Android, the server reports an uncertain
  execution and does not resend the payload blindly. A fallback is used only when accessibility
  proves that no text changed (or the adapter reports a definitive pre-dispatch failure).
- Clear-text attempts are bounded by `max_characters`. An empty-field result is marked verified
  only when Android accessibility exposes a non-password field whose text is demonstrably empty;
  masked or unavailable fields remain unverified.
- App discovery currently returns package names rather than localized display names.
- Screenshot tools return visible pixels as MCP image content; they do not extract or download an
  App's original remote media file.
- Physical-device and emulator compatibility still requires validation across Android versions,
  OEM ROMs, and input methods.

## Attribution

Android controller and selector behavior is derived from
[`minitap-ai/mobile-use`](https://github.com/minitap-ai/mobile-use), licensed under the Apache
License 2.0. The Android-derived implementation was significantly modified and reorganized for
this standalone local stdio server. See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE), and the
auditable [`PROVENANCE.md`](PROVENANCE.md) inventory for attribution and review boundaries.

`mobile-use-mcp` is independently maintained and is not affiliated with, sponsored by, or
endorsed by Minitap, Inc. or the `minitap-ai/mobile-use` project.
