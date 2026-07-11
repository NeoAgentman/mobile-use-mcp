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
- Host-agent task: pending because the local Claude Code installation is not authenticated
  (`Not logged in · Please run /login`).
- No mobile-use MCP protocol incompatibility was observed before the authentication gate.

## Device behavior

- Native MCP image snapshot: passed (`1080x2244`).
- UI hierarchy and selector tap: passed.
- Swipe, Back, Home, and Enter: passed.
- App launch, termination, foreground detection, and HTTP URL opening: passed.
- Unicode text `MCP 你好 & 123`: exact match passed.
- Input method restoration: passed; the user's Baidu input method was restored after automation.

