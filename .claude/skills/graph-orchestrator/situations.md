# Flowstate situations and command errors

Reference for the graph-orchestrator skill. Everything here is produced by the current runtime
(`orchestrator/lib/flowstate/`). "Moves" are the `options` Flowstate returns; `pause` is always
available while the run is active.

Worker directories live at `<run_dir>/workers/<worker_id>/` (`prompt.md`, `transcript.jsonl`,
`stderr.log`, `invocations/NNN/`). Worker ids are `<node>` at the top level, `<node>.<branch_id>` in
a branch, with `.respawn-N` appended for replacements. `run_dir` comes from `flowstate status`.

## Finished and waiting

| Situation | Meaning | Key fields | Moves | Default |
|---|---|---|---|---|
| `completed` | Reached the done node | `variables` | — | Stop, report |
| `aborted` | Run was aborted | `reason` | — | Stop, report |
| `paused` | Run is paused (command or `pause_at` checkpoint) | `reason`, `source` (`command` \| `pause_at`), `paused_at`, `node`/`branch_id` for `pause_at` | resume, abort | Stop; resume only on human instruction |
| `worker_running` | Agent still working when `--max-wait` elapsed | `node`, `worker_id`, `session_id`, `idle_s` | advance | Advance again |
| `script_running` | Script still running when `--max-wait` elapsed | `node`, `pid` | advance | Advance again |
| `branches_running` | Branches still running when `--max-wait` elapsed | `parallel_node`, `join`, `branches` | advance | Advance again |
| `busy` | Another `advance` holds the run's lease | `message` | advance later | Wait ~30 s, up to 3 times, then report |

## Worker and output problems

| Situation | Meaning | Key fields / evidence | Moves | Default |
|---|---|---|---|---|
| `validation_failed` | Outputs missing, not JSON, schema-invalid, wrong `_session_id`, or a `sets_variables` value missing/mistyped | `errors[]` (`file`, `path`, `error` = `missing` \| `invalid_json` \| `schema` \| `session_id_mismatch` \| `binding_failed` \| `binding_type`, `at`, `message`, `expected`, `actual`); `evidence.rejected_outputs` | retry, respawn (agent), abort | Retry with precise feedback; pause if the worker lacks information |
| `gate_failed` | A deterministic check on the chosen edge exited non-zero | `edge`, `gate`, `exit_code`, `timed_out`, `stderr_tail`; `evidence.stdout`, `evidence.stderr` | retry, respawn (agent), abort | Agent node: retry with the gate's requirement. Script node: pause unless transient |
| `worker_failed` | Worker ended badly or could not start | `worker_state` (`failed` \| `killed` \| `lost` \| `launch_failed` \| `send_failed`), `exit_code`, `subtype` (e.g. `error_max_budget_usd`), `result_text`, `stderr_tail`, `message`; `evidence.worker_dir` | retry, respawn, abort | Killed/lost/transient: retry. Budget exhausted, launch failure, or repeated failure: pause |
| `worker_stalled` | No transcript growth for `stall_after_s` (not persisted) | `idle_s`, `stall_after_s`, `worker_id`, `session_id`; `evidence.worker_dir` | advance `--stall-after`, respawn, abort | Wait once with a longer threshold, then respawn |
| `worker_timeout` | Node `timeout` exceeded (not persisted) | `timeout_s`, `worker_id`; `evidence.worker_dir` | advance, respawn, abort | Respawn, or pause if timeouts repeat |
| `retries_exhausted` | The node's budget is used up | `max_retries`, `previous`, `errors` | abort | Pause for a human |

## Deterministic code

| Situation | Meaning | Key fields / evidence | Moves | Default |
|---|---|---|---|---|
| `script_failed` | Script node exited non-zero or timed out | `exit_code`, `timed_out`, `stderr_tail`; `evidence.stdout`, `evidence.stderr` | retry, abort | Pause unless the cause is transient |
| `node_interrupted` | Script runner vanished without a result | `message`; `evidence.log_dir` | retry, abort | Retry once, then pause |
| `reducer_failed` | Join reducer exited non-zero or timed out | `node` (the join), `parallel_node`, `branch_id`, `fold`, `exit_code`, `stderr_tail`; `evidence.log_dir`, `evidence.stderr` | retry (on the join, no `--branch`), abort | Pause unless transient |
| `reducer_output_invalid` | Reducer wrote no summary or a non-object | `message`, `branch_id`, `fold`; `evidence.log_dir` | retry (join), abort | Pause (code defect) |
| `reducer_interrupted` | Reducer runner vanished without a result | `message`, `branch_id`, `fold`; `evidence.log_dir` | retry (join), abort | Retry once, then pause |

## Definition, input and routing problems

| Situation | Meaning | Key fields | Moves | Default |
|---|---|---|---|---|
| `render_failed` | A prompt, path or working directory could not be rendered | `message` | abort | Pause |
| `condition_error` | An edge condition could not be evaluated | `edge`, `condition`, `message` | abort | Pause |
| `no_route` | No outgoing condition was true | `conditions` | abort | Pause |
| `ambiguous_route` | More than one outgoing condition was true | `edges` | abort | Pause |
| `fanout_invalid` | A `dynamic_fanout` items value is not a list, unreadable, or over `max_items` | `node` (the fan-out), `message` | abort | Pause |
| `flow_changed` | Flow files changed after `init` (or no longer load) | `message`, `details` | abort | Pause; never edit files back yourself |

## Branch fields

Present on situations from nodes inside a fork/dynamic_fanout: `parallel_node`, `branch_id`,
`branch_index`, `item` (fan-out only), and on the situation `advance` returns, `branches`
(`total`, `pending`, `running`, `awaiting_decision`, `completed`) and `other_branch_situations`.
Their `options` read `retry --branch <id>` / `respawn --branch <id>`.

## Command errors (`{"error": {...}}`, exit 1)

| Code | From | Meaning | What to do |
|---|---|---|---|
| `not_retryable` | retry | The node has no retryable situation (in that scope) | Re-read the situation; you may have the wrong node or branch |
| `worker_busy` | retry | The worker is still running or stalled | Advance (wait) or respawn |
| `not_respawnable` | respawn | Not an agent node, or the situation cannot be solved by respawn | Choose another action |
| `branch_required` | retry, respawn | Several branches match; `details.branches` lists them | Pass `--branch` from the situation |
| `unknown_branch` | retry, respawn | That branch id does not exist | Use the id from the situation or `status` |
| `not_in_branch` | retry, respawn | `--branch` given for a node outside any region (including join/reducer situations) | Drop `--branch` |
| `no_branches` | retry, respawn | The region has not created branches yet | Advance first |
| `run_busy` | retry, respawn | Another `advance` holds the lease | Wait, then try once more |
| `run_finished` | retry, respawn, pause, resume, abort | Run is completed or aborted | Stop and report |
| `not_paused` | resume | The run is not paused | Advance instead |
| `unknown_node` | retry, respawn | No such node in the flow | Use `node` from the situation |
| `run_not_found`, `flow_not_found` | any | Wrong id, path or `--runs-dir` | Check the id and `--runs-dir` |
| `invalid_flow` | validate, init | Static validation failed (`details.issues`) | Report the issues; do not edit the flow |
| `missing_variable`, `unknown_variable`, `not_an_input`, `invalid_variable` | init | Run inputs are missing or wrong | Ask the human for correct inputs |
| `run_exists` | init | That run id is taken | Use the existing run or a new id |

`retry` and `respawn` can also *return* the `retries_exhausted` situation (exit 0) instead of acting.
