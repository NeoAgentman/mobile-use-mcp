# Android compatibility evidence

The JSON files in `evidence/` are release evidence, not a general device
inventory. Each file is produced by `scripts/android_compatibility_evidence.py`
from an explicitly selected Android serial and contains only stable metadata,
capability statuses, and SHA-256 artifact digests. Complete serials, screenshots,
UI content, typed text, credentials, host paths, and raw diagnostics are not
retained.

## Repeat a physical-device run

The fixture APK is built once by the `Build Android fixture artifact` CI check.
Download that artifact and the matching `mobile-use-mcp-distributions-<commit>`
artifact from the same commit. Do not build the fixture APK locally for release
evidence. The physical harness verifies both artifacts before it starts the
device flow. If verifying manually, use the platform's SHA-256 checker (for
example, `shasum -a 256 -c fixture.apk.sha256` on macOS or
`sha256sum --check fixture.apk.sha256` on Linux):

```bash
(cd fixture-artifact && sha256sum --check fixture.apk.sha256)
uv run python scripts/artifact_digest.py \
  --verify --directory distributions --output distributions/wheel.sha256
```

Install the wheel into a dedicated environment and the CI-built APK onto one
explicitly authorized device. Then run:

```bash
uv run python scripts/android_compatibility_evidence.py \
  --serial "$MOBILE_USE_ANDROID_SERIAL" \
  --matrix-entry physical-huawei-api-29 \
  --server-command "/path/to/wheel-env/bin/mobile-use-mcp" \
  --wheel "/path/to/distributions/mobile_use_mcp-0.4.0-py3-none-any.whl" \
  --wheel-digest "/path/to/distributions/wheel.sha256" \
  --wheel-manifest "/path/to/distributions/wheel.manifest.json" \
  --fixture-apk "/path/to/fixture-artifact/app-debug.apk" \
  --fixture-digest "/path/to/fixture-artifact/fixture.apk.sha256" \
  --fixture-manifest "/path/to/fixture-artifact/fixture.apk.manifest.json" \
  --evidence compatibility/evidence/physical-huawei-api-29.json
```

The command requires `--serial`; it never discovers a device or chooses the
first connected device. It runs the same installed-wheel public `ClientSession`
flow as emulator CI and uses bounded stage/total deadlines. The outer runner
reaps the server process and the public flow always resets the fixture, returns
Home, stops recording when needed, and disconnects. Both CI manifests are
mandatory and must identify the same source commit; a digest match alone is
not enough.

The flow measures the delayed fixture wait with a monotonic clock and exercises
the public recording start/status/stop transition. A recording-capable device
must complete that transition; an unsupported device must return structured
`UNSUPPORTED`. Slow-device and USB-disconnect evidence are never command-line
claims. USB recovery is opt-in and needs an operator to perform the physical
condition:

```bash
uv run python scripts/android_compatibility_evidence.py \
  ...same arguments as above... \
  --exercise-usb-disconnect
```

When the USB option is omitted, the record says `not_tested`; release validation
does not treat it as a support claim. With the option, the harness prompts the
operator to unplug and reconnect the selected serial, observes the disconnect
through ADB, requires a public snapshot failure, then reconnects through public
MCP tools and verifies `android_status`.

## Validate and render

Validate fresh records and require named entries during a release audit:

```bash
uv run python scripts/compatibility_matrix.py validate \
  --evidence-dir compatibility/evidence \
  --matrix-config compatibility/matrix.json
uv run python scripts/compatibility_matrix.py render \
  --evidence-dir compatibility/evidence \
  --output COMPATIBILITY.md \
  --matrix-config compatibility/matrix.json
```

The full release matrix is intentionally larger than the hardware currently
available to the operator. See `matrix.json` for the named entries and the
remaining device classes needed before those claims can be made.
