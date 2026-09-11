## Phase 2: Labeling ({N_RUNS} parallel subagents)

Given the fixed candidates from Phase 1, run {N_RUNS} independent labeling runs in
parallel using **subagents**. Each subagent gets a fresh, isolated context containing only
the Phase 2 prompt, the candidates from Phase 1, and access to the eval files.

Because labeling each (trajectory, capability) pair as NA / PRESENT / LACKING is a
genuinely difficult judgment call (especially the PRESENT vs LACKING distinction on failed
trajectories), independent subagents will disagree on borderline cases. That cross-run
disagreement is exactly what the consistency filter measures.

### Why subagents instead of sequential runs

- **True independence:** A single agent doing 10 sequential runs would see all prior runs
  in its context, biasing later runs. Subagents have zero shared context.
- **Parallelism:** All {N_RUNS} runs finish in roughly the time of one.
- **Single command:** The orchestrating agent fans out, collects results, and runs the
  aggregation script — the user only issues one instruction.

### Agent Prompt — Phase 2

Copy the prompt below, fill in the `{PLACEHOLDERS}`, and pass it to each subagent.

```
You are a contrastive capability labeling agent. You are given evaluation results from a
model called {MODEL_NAME} and a fixed set of candidate capabilities. Your job is to label
every (trajectory, capability) pair so we can compute coverage and contrastive gap metrics.

The evaluation results are located at: {EVAL_RESULTS}

The candidate capabilities (from a prior discovery phase) are:

{CANDIDATE_CAPABILITIES_JSON}

## Instructions

For each trajectory in the evaluation results AND each candidate capability, assign one of
three labels:

- **NA** — This capability is NOT APPLICABLE to this task. The task does not require this
  capability for successful completion.

- **PRESENT** — This capability IS APPLICABLE to this task, AND the agent successfully
  demonstrated it. The agent did exhibit the capability when it was needed.

- **LACKING** — This capability IS APPLICABLE to this task, AND the agent FAILED to
  demonstrate it. The agent did not exhibit the capability when it was needed.

The crucial distinction is between PRESENT and LACKING on FAILED trajectories: a trajectory
can fail for many reasons. For each capability, you must judge whether the failure was
specifically due to lacking THAT capability, or whether the agent had that capability fine
but failed for unrelated reasons.

Examples:
- A failed trajectory where the agent never needed numerical reasoning at all
  → numerical_reasoning is NA
- A failed trajectory where the agent did the math correctly but called the wrong tool
  → numerical_reasoning is PRESENT (the failure was due to a different capability)
- A failed trajectory where the agent computed the wrong total and that caused the failure
  → numerical_reasoning is LACKING

For SUCCESSFUL trajectories, the labels are still meaningful:
- A successful trajectory where the capability wasn't needed → NA
- A successful trajectory where the capability was needed and the agent did it → PRESENT
- A successful trajectory where the capability was needed but the agent did it wrong yet
  somehow succeeded anyway → LACKING (rare but possible)

## Output Format

Return ONLY a JSON object with this exact structure (no other text):
{
  "attempt": {ATTEMPT_NUMBER},
  "totals": {
    "total_failed": <int — total number of failed trajectories you analyzed>,
    "total_passed": <int — total number of successful trajectories you analyzed>
  },
  "labels": {
    "capability_name_1": {
      "lacking_failed": ["task_id_a", "task_id_b", ...],
      "present_failed": ["task_id_c", ...],
      "na_failed":      ["task_id_d", ...],
      "lacking_passed": ["task_id_e", ...],
      "present_passed": ["task_id_f", ...],
      "na_passed":      ["task_id_g", ...]
    },
    "capability_name_2": { ... },
    ...
  }
}

## Important Guidelines

- Use the EXACT capability names from the candidates — do not rename or invent new ones
- Every trajectory must be labeled for every capability — no skipping
- The six lists for each capability are a partition: each task ID appears in exactly ONE
  of the six lists (lacking_failed | present_failed | na_failed | lacking_passed |
  present_passed | na_passed)
- For each capability, the sum of (lacking_failed + present_failed + na_failed) must equal
  totals.total_failed, and the sum of the three "_passed" lists must equal totals.total_passed
- Be honest about borderline cases. If you genuinely cannot tell whether a capability is
  PRESENT or LACKING on a failed trajectory, lean toward PRESENT — false LACKING labels
  inflate Cov and Δ artificially
- Different subagents will disagree on borderline cases. That's expected and is exactly
  what the cross-run consistency check measures
```

### Running Phase 2 with subagents

The orchestrating agent should spawn {N_RUNS} subagents in parallel — all in a single
message so they execute concurrently — using the `Agent` tool.

For each subagent `i` from 1 to {N_RUNS}:

1. Substitute `{ATTEMPT_NUMBER}` with `i` in the Phase 2 prompt above
2. Substitute `{CANDIDATE_CAPABILITIES_JSON}` with the contents of
   `{OUTPUT_DIR}/candidate_capabilities.json` from Phase 1
3. Pass the substituted prompt to the subagent and instruct it to write its output JSON
   to `{OUTPUT_DIR}/run_{i:02d}.json`

Each subagent will:
- Read the eval result files from the paths in the prompt
- Independently label every (trajectory, capability) pair
- Write its result JSON to the specified path
- Return a brief confirmation to the orchestrator

---\n