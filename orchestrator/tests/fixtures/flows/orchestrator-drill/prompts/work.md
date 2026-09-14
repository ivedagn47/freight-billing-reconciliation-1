You are a step worker in an orchestrator drill. You handle exactly one step.

Step: {item}
Branch: {_branch_id}

Write a JSON file at exactly this path: {report}

The JSON must be:

{
  "_session_id": "<your session id>",
  "step": "<the step above, exactly>",
  "branch_id": "<the branch above, exactly>",
  "done": true
}

"done" must be the JSON boolean true, not a string.
