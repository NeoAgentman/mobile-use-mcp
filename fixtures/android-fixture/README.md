# Android MCP fixture

This is a small, dependency-free Android application used by the public stdio acceptance flow. It
uses only platform `Activity` and `View` classes so the same APK can run on AOSP emulators and
OEM devices without relying on an OEM launcher, theme, or widget library.

The fixture package is `com.neoagentman.mobileusefixture`. The following resource IDs are stable
and are part of the acceptance contract:

| Resource ID | Purpose |
| --- | --- |
| `fixture_title` | Stable title and launch-ready marker |
| `fixture_status` | Human-readable state/status |
| `fixture_revision` | Fixture-local monotonic state revision |
| `fixture_change_button` | Deterministic UI-change trigger |
| `fixture_change_value` | `UI state: baseline`/`UI state: changed` result |
| `fixture_delayed_trigger` | Starts the delayed-element transition |
| `fixture_delayed_element` | Hidden until the bounded delayed transition completes |
| `fixture_text_input` | Single-line Unicode-capable input field |
| `fixture_reset_button` | Returns the activity to its baseline state |
| `fixture_scroll_container` | Scrollable container with 32 deterministic rows |
| `fixture_scroll_state` | Indicates top/moved scroll state |

CI builds and publishes the debug APK as the `mobile-use-mcp-android-fixture-<commit>` artifact,
including `fixture.apk.sha256` and `fixture.apk.manifest.json`. For a release-evidence run, download
that exact artifact and verify both files before installing it. The manifest must identify the same
commit as the wheel manifest. A local build is useful for fixture development, but must not replace
the CI artifact in compatibility evidence.

For local fixture development, build and install the debug APK with an Android SDK and Gradle:

```bash
gradle --no-daemon --console plain assembleDebug
adb -s "$MOBILE_USE_ANDROID_SERIAL" install -r app/build/outputs/apk/debug/app-debug.apk
```

The acceptance command requires an explicit serial and launches an independently installed
`mobile-use-mcp` server through the official Python MCP `ClientSession`:

```bash
uv run python scripts/android_fixture_acceptance.py --serial "$MOBILE_USE_ANDROID_SERIAL"
```

The core acceptance flow does not import `mobile_use_mcp.server`, access a controller, or call
`adb` after it starts the MCP server. It uses public `android_*` tools for observation, actions,
condition waits, reset, Home, recording, and disconnect. The optional, explicitly requested
`--exercise-usb-disconnect` operator phase is the sole exception: its outer harness uses bounded
`adb get-state` probes to observe the physical unplug/replug while the disconnect error and
recovery are still verified through public MCP tools. See the script's `--help` output for
selecting an installed server command or changing the one total deadline.
