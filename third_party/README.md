# Third-party research assets

This directory vendors the upstream assets used by TraceForge's failure-analysis adapter.

- `agentrx/agentrx/judge/judge.py` from Microsoft AgentRx, commit `f228165b`.
- `trace/prompts/general/*.md` from Scaling Intelligence Lab TRACE, commit `d2db2308`.
- Licenses are reproduced under `third_party/licenses/`.

The runtime adapter does not execute AgentRx generated Python in-process and does not treat TRACE pass/fail contrastive metrics as available when outcome labels are missing. It reuses the root-cause scan, taxonomy/checklist, discovery and capability-labeling prompt structures while preserving TraceForge evidence references and Harbor isolation.
