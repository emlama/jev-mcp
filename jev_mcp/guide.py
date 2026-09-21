"""The agent guide served by the `get_guide` tool and the `jev://guide` resource.

The guide is TypeSafe's official agent skill (MIT, vendored verbatim under
``jev_mcp/vendor/typesafe-ai``) with a short adapter on top that maps the
skill's concepts onto this server's tools. Refresh the vendored copy with
``uv run python scripts/update_skill.py``.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

GUIDE_URI = "jev://guide"

ADAPTER = """\
# Using TypeSafe Jev through jev-mcp

This server stores reusable TypeSafe Jev queries ("jev tools") so any authorized agent can
rerun them in later sessions. The document below this section is TypeSafe's official agent
skill. It teaches how to design judgments; this section explains how its concepts map onto
the tools you have here.

## How the skill's concepts map to jev-mcp

| In the skill | In jev-mcp |
| --- | --- |
| `state` sent to the API | For `ask_jev`: the `state` argument, verbatim. For a saved tool: the tool's `context` (constant reference material) merged with the caller's `inputs` (declared per tool), so every input name and context key becomes a top-level state field. |
| `questions` map | The same JSON, passed unchanged to `ask_jev` or stored by `create_tool`. |
| `client.system_one(...)` / `POST /v1/systemone` | `ask_jev` for one-off calls; `run_tool` for a saved tool. The server holds the API key; you never see it. |
| Backticked paths such as `` `ticket.messages[0].text` `` | Must start with a declared input name or a top-level context key; `create_tool` rejects anything else and names the offending path. |
| "Read the live docs" | You may not be able to fetch URLs from this client. Everything you need for the request shape is in this document and in the tool descriptions. |

## Question shapes (the part agents most often get wrong)

```json
{
  "urgent":   {"type": "noul",   "instructions": "Does `email.body` express urgency?",
               "criteria": {"true": "explicitly time-sensitive", "false": "no urgency"}},
  "dept":     {"type": "choice", "instructions": "Which team should handle `email`, given `policy`?",
               "criteria": {"billing": "payments and refunds", "support": null, "sales": "pricing"}},
  "anger":    {"type": "score",  "instructions": "How angry is `email.body`?",
               "criteria": ["calm", "irritated", "angry"]}
}
```

- noul: `criteria` is optional; the answer is `noul`, the probability of yes.
- choice: `criteria` keys are the option names (2 to 255); a `null` description is allowed. The answer has
  `choice`, `probabilities` per option, and `confidence`.
- score: `criteria` is an ordered list of 2 to 10 level descriptions; the answer has `score`
  (probability-weighted position, may fall between levels), `probabilities` per level, `legend`, and
  `confidence`.

## Workflow

1. `list_tools`, then `get_tool` to read a tool's docs before `run_tool`. Reuse before you rebuild.
2. Design with `ask_jev` on real examples until the answers are right. Nothing is saved.
3. `create_tool` with: a slug `name`; `inputs` (name to `{type: string|object|array, description,
   required?}`); optional `context` for policies, definitions, and worked examples; the `questions`; and
   `docs` that state purpose, when to use it, how to read each answer, and the thresholds you settled on.
4. `run_tool` in this or any later session. `tool_runs` shows every call with inputs, answers, usage,
   latency, and the calling agent, so you can audit and tune. `update_tool` bumps the version;
   `delete_tool` removes a tool but keeps its run history.

Limits enforced by the server: names match `^[a-z][a-z0-9_]{2,63}$`; context plus questions under
200 KB; run inputs under 256 KB; the model's own budget is 64k tokens per request.

---

# TypeSafe agent skill (upstream, verbatim)

"""


@cache
def load_guide() -> str:
    """The adapter followed by the vendored upstream skill and its license notice."""
    vendor = resources.files("jev_mcp").joinpath("vendor", "typesafe-ai")
    skill = vendor.joinpath("SKILL.md").read_text(encoding="utf-8")
    license_text = vendor.joinpath("LICENSE").read_text(encoding="utf-8")
    source = vendor.joinpath("SOURCE.txt").read_text(encoding="utf-8")
    return (
        f"{ADAPTER}{skill.rstrip()}\n\n---\n\n"
        f"The section above is reproduced from the TypeSafe skills repository under the MIT license.\n"
        f"{source.strip()}\n\n{license_text.strip()}\n"
    )
