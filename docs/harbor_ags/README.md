# Harbor AGS runtime pin

TraceForge executes the Harbor AGS runtime from the external `harbor_root`.
The rollout plan records the SHA-256 of `src/harbor_ags/evidence.py` and
refuses to execute if that file changes after plan creation.

The checked-in patch `evidence-compaction-binding.patch` is applied to the
runtime source at `/mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags`.
It binds Hermes assistant messages to capture exchanges by `tool_call_id`,
keeps compacted capture calls as raw evidence with warnings, and computes
ATIF usage from represented calls while retaining full raw usage separately.

Runtime evidence.py SHA-256:
`98fdc7fc0e98b9c298571d2483d286fc72be16776cab140eb3e718d54846aeba`
