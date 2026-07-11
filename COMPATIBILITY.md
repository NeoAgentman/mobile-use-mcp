# Host compatibility

Verified on 2026-07-11 with Android device `HUAWEI P20` (`EML-AL00`, HarmonyOS 3.0.0).

## Generic MCP clients

- Python MCP `ClientSession`: passed initialize, list tools, connect, native image snapshot,
  structured UI hierarchy, actions, and disconnect.
- MCP Inspector: passed tool discovery and tool calls.

## Codex

- MCP server configuration: enabled and discovered.
- Host-agent task: passed.
- Task: connect the phone, open Settings, navigate to About phone, read Model name and Model
  number, press Home, and disconnect.
- Result: `HUAWEI P20`, `EML-AL00`.
- The Codex agent recovered from one failed selector by taking a new snapshot and trying another
  target, demonstrating that structured MCP errors are usable by the host agent.

## Claude Code

- MCP health check: connected.
- The stock `claude` command was not authenticated, so host-agent validation used the user's
  Claude Code-compatible `cc-ark` zsh function.
- `cc-ark` host-agent task: passed.
- Task: connect the phone, open Settings, navigate to About phone, read Model name and Model
  number, press Home, and disconnect.
- Result: `HUAWEI P20`, `EML-AL00`.
- No mobile-use MCP protocol incompatibility was observed.

## Device behavior

- Native MCP image snapshot: passed (`1080x2244`).
- UI hierarchy and selector tap: passed.
- Swipe, Back, Home, and Enter: passed.
- App launch, termination, foreground detection, and HTTP URL opening: passed.
- Unicode text `MCP 你好 & 123`: exact match passed.
- Input method restoration: passed; the user's Baidu input method was restored after automation.
- Physical USB disconnect during an active MCP session: passed; the next device operation returned
  structured `DEVICE_DISCONNECTED` with a reconnect suggestion.

## Version 0.2 additions

- PNG/JPEG screenshot encoding was introduced here; optional resizing was later removed in 0.3.1.
- App launch results include structured attempt and foreground-blocker diagnostics.
- Bounded recording, segment rollover, stop, transfer, missing-ffmpeg fallback, and disconnect
  cleanup are covered by non-device tests. Real-device recording remains an opt-in verification.
- The verified HUAWEI P20 HarmonyOS build does not include Android's `screenrecord` binary; the
  MCP returns structured `UNSUPPORTED` instead of repeatedly restarting a failed process.

## Version 0.3 additions

- Compact snapshots expose explicit truncation counts, continuation offsets, and a full fallback.
- The latest full normalized hierarchy is addressable through an opaque `snapshot_id`.
- UI element reads support stable pagination, keyword query, package filtering, and
  interactive-only filtering against the same cached snapshot.
- State-changing operations invalidate cached IDs and stale reads return `SNAPSHOT_NOT_FOUND`.
- The complete stdio snapshot-to-pagination flow passed on the verified HUAWEI P20 device.

## Version 0.3.1 screenshot defaults

- Snapshot and screenshot tools always preserve the physical device resolution.
- The default image is JPEG quality 60; lossless PNG remains an explicit retry option.
- JPEG responses expose both a human-readable quality notice and machine-readable PNG fallback.
- MCP schemas no longer expose a screenshot resizing parameter.
- The default original-resolution JPEG and PNG fallback metadata passed real-device stdio testing.

## Current main after 0.3.1

- MCP initialize now exposes service-level instructions for natural-language Android/mobile-App
  requests, including Chinese phrases such as `手机 App`, `打开某个 App`, `查看评论`, and
  `采集页面数据`.
- Snapshot and action schemas describe compact/full selection, pagination-first fallback,
  per-node text limits, interactive-only filtering, Target field mapping, fixed-delay waits, and
  post-action observation requirements.
- Stdio tests verify that the service instructions and key schema descriptions are delivered to an
  MCP client rather than existing only in README documentation.
- A Codex subagent completed a real Idle Fish (`闲鱼`) workflow using compact snapshots: search for
  `校招`, open the first three results, and extract their descriptions. It did not use full mode or
  reuse stale snapshot IDs. Schema validation rejected two invalid arguments and the agent recovered
  by correcting them.
- App discovery still searches third-party package names rather than localized launcher labels.
- Screenshot tools provide visible pixels as MCP image content; downloading an App's original
  remote image asset is not currently a supported capability.
