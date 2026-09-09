---
name: km
description: "Configure and inspect knowledge-vault recall for interviews and seed generation"
---

# /ouroboros:km

Configure and inspect knowledge-vault recall used by interview and PM advisory `km_context` lanes.

## What it does

Recall existing notes from the user's knowledge vault that may already answer an interview or seed-generation question. The `km_context` lane reports **paths and one-line summaries only** — never note bodies, never a stand-in answer.

Hits are capped at 3. Each hit carries `path`, `one_line` (max 200 chars), `score`, and `tier` (`graphrag` | `obsidian_cli` | `text`). If this host has no vault-search means, the lane returns `{"question_identity":"<the question's identity>","lane_id":"km_context","hits":[]}`.

This skill has no MCP tool. Configuration is host-local; recall is executed by the parent session with whatever vault-search means the host already exposes.

## Configuration

Read and write `km:` under `~/.ouroboros/config.yaml`. Five keys:

| Key | Meaning |
|-----|---------|
| `enabled` | Whether vault recall is on for interview/seed advisory |
| `endpoint` | GraphRAG (or compatible) search URL |
| `vault_path` | Local vault root, when a filesystem fallback is used |
| `top_k` | How many hits to request from the search means (lane still returns at most 3) |
| `timeout_seconds` | Per-search timeout |

Example:

```yaml
km:
  enabled: true
  endpoint: http://127.0.0.1:8400
  vault_path: ""
  top_k: 5
  timeout_seconds: 20
```

Endpoint auto-detect, in this order — first hit wins:

1. `km-config.json` (cwd, then `~/.claude/km-config.json`) — use its endpoint/URL field when present
2. `GRAPHRAG_API_URL` environment variable
3. `http://127.0.0.1:8400`

An explicit `km.endpoint` in `config.yaml` overrides auto-detect. `enabled: false` disables the lane's search; the contracted empty-hits answer still applies.

## Status

Run `ouroboros config show` and look at the `km` block (endpoint, vault_path).

When the user asks for km status, report the resolved values, not the raw file:

- `enabled`
- resolved `endpoint` (and which auto-detect step supplied it, if any)
- `vault_path`
- `top_k`
- `timeout_seconds`
- whether a search means is actually callable from this host

Do not claim the vault is reachable from `readyz`/`health` alone. Reachability is a successful search (or a documented empty-hits no-means result).

## Manual recall once

When the user supplies a query (or an interview question to recall against):

1. Confirm km is enabled and a search means exists. If not, return `{"question_identity":"<the question's identity>","lane_id":"km_context","hits":[]}` and stop.
2. Derive 3–7 keywords from the question.
3. Search with the host's vault-search means (GraphRAG, Obsidian CLI, then text), in that tier order.
4. Reply with the contracted JSON only — at most 3 hits: Copy the question's identity from the request into `question_identity`.

```json
{
  "question_identity": "interview-question:0123456789abcdef",
  "lane_id": "km_context",
  "hits": [
    {
      "path": "path/to/note.md",
      "one_line": "One-line summary of why this note is relevant.",
      "score": 0.0,
      "tier": "graphrag"
    }
  ]
}
```

Do not paste note bodies. Do not turn hits into an interview answer.

`/ouroboros:km "<query>"` is a single recall. Run the Manual recall steps once for that query and stop.

Do **not** start an interview. Do **not** start a PM interview. Do **not** fan out other advisory lanes. Do **not** generate a seed. The output is the contracted `km_context` JSON only.

## Turning it off

Disable vault recall in either of these ways:

- Set `km.enabled: false` in `~/.ouroboros/config.yaml`
- Invoke `/ouroboros:km --off` — applies to the current session only; nothing is written to disk, and the next session reads `config.yaml` again

While off, a recall still returns `{"question_identity":"<the question's identity>","lane_id":"km_context","hits":[]}`. Do not enter an interview or PM flow to “turn it back on”.

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
