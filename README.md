# mobile-use-mcp

`mobile-use-mcp` is a local stdio MCP server that lets coding agents such as Codex and
Claude Code observe and operate Android devices through ADB and uiautomator2.

The server does not contain an LLM and does not require a model API key. The host agent
interprets screenshots and UI elements, plans actions, and calls the exposed MCP tools.

## Scope

- Android physical devices and emulators
- Screenshot and accessibility hierarchy observation
- Tap, long press, swipe, text input, key presses, app lifecycle, and URL opening
- Local stdio MCP transport

The first release intentionally excludes iOS, arbitrary ADB shell commands, APK installation,
app uninstall/data clearing, cloud devices, telemetry, and embedded model providers.

## Status

Initial implementation in progress. See `TODO.md` for the delivery plan.

## Attribution

Android controller and selector behavior is derived from
[`minitap-ai/mobile-use`](https://github.com/minitap-ai/mobile-use), licensed under the
Apache License 2.0. See `NOTICE` for details.

