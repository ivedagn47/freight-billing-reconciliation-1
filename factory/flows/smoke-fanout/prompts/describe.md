You are a worker in a dynamic fan-out smoke test. You handle exactly one item.

Item: {item}
Branch: {_branch_id}

Write a JSON file at exactly this path: {description}

The JSON must be:

{
  "_session_id": "<your session id>",
  "item": "<the item above, exactly>",
  "branch_id": "<the branch above, exactly>",
  "text": "<one sentence describing the item>"
}
