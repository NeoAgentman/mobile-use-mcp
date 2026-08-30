# Source provenance

This inventory records source ancestry for the Android implementation adapted from
[`minitap-ai/mobile-use`](https://github.com/minitap-ai/mobile-use). The reference upstream
revision is:

```text
cc20e3d52bf6dac1dff07f7b894520b8aeb051b7
```

The revision and file mappings are machine-readable in [`provenance.toml`](provenance.toml).
Each entry also records the SHA-256 of the current repository file so the repeatable release
audit can detect an unreviewed source change. A hash change is expected after an intentional
edit; update the inventory and review the corresponding attribution statement in the same
change.

## Derived-file inventory

| Repository file | Upstream path or fragment | Adaptation record |
| --- | --- | --- |
| `src/mobile_use_mcp/android_client.py` | `minitap/mobile_use/clients/ui_automator_client.py` | Significantly rewritten for the local MCP session boundary, configured ADB endpoint, bounded reconnect, and secret-safe text handling. |
| `src/mobile_use_mcp/controller.py` | `minitap/mobile_use/controllers/android_controller.py`; `minitap/mobile_use/tools/utils.py` | Significantly rewritten as a deterministic Android-only controller with typed errors, validation, and MCP-safe results. |
| `src/mobile_use_mcp/selectors.py` | `minitap/mobile_use/tools/utils.py`; `minitap/mobile_use/utils/ui_hierarchy.py` | Significantly rewritten into a small Android selector resolver with bounded normalized targets and an explicit fallback ledger. |
| `src/mobile_use_mcp/snapshot.py` | `minitap/mobile_use/clients/ui_automator_client.py` | Significantly rewritten to normalize and bound Android hierarchy output for the MCP schema. |
| `src/mobile_use_mcp/recording.py` | `minitap/mobile_use/utils/video.py`; `minitap/mobile_use/tools/mobile/video_recording.py` | Significantly rewritten for owned bounded screen-recording processes, segment cleanup, and local artifact limits. |

The files above are the identified upstream-derived implementation files. The remaining modules
were written for this standalone MCP server or use third-party library APIs; they are not claimed
here as copied from the upstream project.

## Attribution and review boundary

The repository carries the upstream Apache License and attribution in [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE). The modification statements above identify significant rewrites in the
listed files. This inventory is a source ancestry and notice record, **not a legal conclusion**;
human legal review remains open for any distribution or trademark questions.

`mobile-use-mcp` is independently maintained and is not affiliated with, sponsored by, or
endorsed by Minitap, Inc. or the `minitap-ai/mobile-use` project.
