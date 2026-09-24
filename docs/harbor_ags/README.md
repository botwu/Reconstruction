# Harbor AGS runtime pin

TraceForge executes the Harbor AGS runtime from the external harbor_root.
The rollout plan records the SHA-256 of src/harbor_ags/evidence.py and
src/harbor_ags/validator.py and refuses to execute if either file changes
after plan creation.

The checked-in patches evidence-compaction-binding.patch and
validator-compaction-binding.patch are applied to the runtime source at
/mnt/afs_toolcall/wujian1/Projects/workspace/harbor_ags. They bind Hermes
assistant messages to capture exchanges by tool_call_id, keep compacted
capture calls as raw evidence with warnings, and compute ATIF usage from
represented calls while retaining full raw usage separately. The validator
skips only positional transcript/count checks that are undefined after
compaction; raw capture completeness and tool-result integrity remain strict. It also
accepts Harbor's native list-shaped artifact manifest after validating bounded
destinations, status, regular files, symlinks, and hardlinks.

Runtime evidence.py SHA-256:
98fdc7fc0e98b9c298571d2483d286fc72be16776cab140eb3e718d54846aeba

Runtime validator.py SHA-256:
9a54deaff2baba525f993d9476a7e8080e2389a30f2d4b4612269019486fb420
