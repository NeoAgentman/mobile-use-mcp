# mobile-use-mcp

`mobile-use-mcp` is a local stdio MCP server that lets coding agents such as Codex and
Claude Code observe and operate Android devices through ADB and uiautomator2.

The server contains no LLM and requires no model API key. The host agent interprets screenshots
and UI elements, plans actions, and calls MCP tools directly.

## Capabilities

- Discover, select, and disconnect Android physical devices and emulators
- Return screenshots as native MCP image content
- Return a compact UIAutomator accessibility hierarchy
- Tap or long press using bounds, resource ID, or text fallbacks
- Swipe using validated screen coordinates
- Type and clear text in the focused field
- Press a bounded set of Android keys
- List, launch, and terminate apps
- Open HTTP and HTTPS URLs
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

## Requirements

- macOS, Linux, or Windows with Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Android SDK platform-tools with `adb` on `PATH`
- A physical Android device with USB debugging enabled, or an Android emulator

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
4. Prefer a target containing bounds plus resource ID or text.
5. Perform one action.
6. Call `android_snapshot` again to verify the resulting state.
7. If an action fails, use the returned selector attempts and refresh the snapshot.

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
| `android_connect` | Select and initialize one device |
| `android_status` | Read current MCP session state |
| `android_disconnect` | Release the active session |

### Observation

| Tool | Purpose |
|---|---|
| `android_snapshot` | Screenshot plus compact UI elements and foreground app |
| `android_screenshot` | Screenshot plus basic metadata |
| `android_get_ui_elements` | UI elements without image content |
| `android_get_foreground_app` | Current package and activity |
| `android_list_apps` | Filter third-party package names |

`android_snapshot` supports `interactive_only`, `max_elements`, and `max_text_length` to control
context size. Screenshots are returned as MCP image content; screenshot base64 is not duplicated in
the structured JSON result.

### Actions

| Tool | Purpose |
|---|---|
| `android_tap` | Tap using bounds, resource ID, or text fallbacks |
| `android_long_press` | Long press a target |
| `android_swipe` | Swipe between validated pixel coordinates |
| `android_type_text` | Optionally focus a target, then type text |
| `android_clear_text` | Optionally focus a target, then clear text with a delete-key fallback |
| `android_press_key` | Press back/home/enter/delete/tab/menu/volume keys |
| `android_launch_app` | Launch a package and poll until foreground |
| `android_terminate_app` | Force-stop a package |
| `android_open_url` | Open an HTTP or HTTPS URL |
| `android_wait` | Wait for a bounded delay before observing again |

## Target selection

Tap and long-press tools accept a `target` object:

```json
{
  "bounds": {"x": 20, "y": 100, "width": 280, "height": 80},
  "resource_id": "com.example:id/continue",
  "resource_id_index": 0,
  "text": "Continue",
  "text_index": 0
}
```

The server tries selectors in this order:

1. Valid on-screen bounds
2. Resource ID and occurrence index
3. Exact case-insensitive text/content description and occurrence index

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
- `ELEMENT_NOT_FOUND`
- `INVALID_COORDINATES`
- `OPERATION_FAILED`
- `TIMEOUT`

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
- UI elements are snapshots, not stable DOM nodes. Observe again after navigation, animation, or
  scrolling.
- `android_type_text` and `android_clear_text` accept an optional target. Without one, they operate
  on the currently focused field.
- App discovery currently returns package names rather than localized display names.
- Physical-device and emulator compatibility still requires validation across Android versions,
  OEM ROMs, and input methods.

## Attribution

Android controller and selector behavior is derived from
[`minitap-ai/mobile-use`](https://github.com/minitap-ai/mobile-use), licensed under the Apache
License 2.0. See `LICENSE` and `NOTICE` for attribution and license details.
