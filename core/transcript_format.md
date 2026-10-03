# Transcript format, v1

What a model sees of a conversation, stored one message per row in
`thread_messages.data` (`core/transcript.py` encodes and decodes it). It
replaces the LangGraph checkpoint as the record of a thread, and it is the
contract between the Python agent loop and the Rust one that follows it: both
read and write this JSON, so a thread can move between them.

Two rules shape it:

- **Lossless.** A LangChain message encoded and decoded is equal to the
  original — `tests/test_transcript.py` checks every shape, and
  `main.py maintenance check-transcript` checks a real `checkpoints.db`.
- **Neutral where it matters, opaque elsewhere.** What a loop or a provider
  adapter acts on has a field of its own. What one provider attached for
  itself rides along in `extras`, untouched, for that provider to read back.

## Message

```json
{
  "v": 1,
  "role": "assistant",
  "id": "run-…",
  "name": "main",
  "content": "…" | [Part, …],
  "tool_calls": [{"id": "…", "name": "run_cell", "args": {…}, "signature": "…"}],
  "invalid_tool_calls": [{"id": "…", "name": "…", "args": "…", "error": "…"}],
  "usage": {"input": 812, "output": 40, "total": 852,
            "input_details": {"cache_read": 700}, "output_details": {"reasoning": 12}},
  "model": {"provider": "google_genai", "name": "gemini-2.5-pro"},
  "finish_reason": "STOP",
  "extras": {"additional_kwargs": {…}, "response_metadata": {…}}
}
```

| field | roles | |
|---|---|---|
| `v` | all | format version; a reader refuses a version it doesn't know |
| `role` | all | `user`, `assistant`, `tool`, `system` |
| `id` | all | the message's own id; absent when it had none |
| `name` | all | absent when none |
| `content` | all | a string, or a list of parts |
| `tool_calls` | assistant | `signature`: Gemini's thought signature for that call, to be sent back with it |
| `invalid_tool_calls` | assistant | calls the model emitted but that didn't parse; `args` is the raw text |
| `usage` | assistant | token counts as the provider reported them. `input_details` / `output_details` keep LangChain's standard keys (`cache_read`, `cache_creation`, `reasoning`, `audio`) |
| `model` | assistant | `provider` is the catalog's provider id; either key may be absent |
| `finish_reason` | assistant | as the provider spelled it |
| `tool_call_id`, `status` | tool | `status` is `success` or `error` (an MCP or tool failure) |
| `artifact` | tool | a tool's non-model output, any JSON |
| `extras` | all | whatever else the message carried: `additional_kwargs` and `response_metadata` leftovers, for the provider that wrote them |

Absent and empty are the same: an encoder omits an empty list or map.

## Parts

A part is a string — text with nothing attached — or an object with a
`type`. Every object part may carry `extras`: keys the provider attached to
that part (a stream `index`, an OpenAI item `id`, `phase`, `cache_control`).

| type | fields | |
|---|---|---|
| `text` | `text`, `signature`? | `signature`: Gemini's thought signature on a text part |
| `thinking` | `thinking`, `signature`? | model reasoning; `signature` is Anthropic's |
| `redacted_thinking` | `data` | Anthropic's encrypted reasoning |
| `image` | `mime_type`, and `blob` or `data` or `url` | |
| `file` | `mime_type`, and `blob` or `data` or `url` | any other media (audio, PDF, …) |
| `opaque` | `data` | a provider-specific part kept verbatim: OpenAI `reasoning` / `function_call` items, Anthropic `tool_use`, Bedrock `reasoning_content`, … — in place, because order matters to the provider that wrote it |

`blob` is `sha256:<hex>` of the bytes, which live once in `transcript_blobs`.
`data` is base64 kept inline — only when the original base64 wasn't canonical
and couldn't be rebuilt byte for byte from the bytes. `extras.lc_type` records
which LangChain spelling (`image_url`, `media`) an image or file came in.

Thinking from one provider must not reach another (`core/messages.py:
strip_historical_thinking`): `thinking`, `redacted_thinking`, and `opaque`
parts from a different `model.provider` are dropped before a call.

## Tables (`database.db`)

| table | |
|---|---|
| `thread_messages` | `id` (row), `thread_id`, `seq` (order in the thread, unique per thread), `message_id`, `role`, `data` (this format), `evicted_at` (compacted away: kept for episodes, not sent), `created_at` |
| `thread_state` | `thread_id`, `todos` (JSON), `source` (`checkpoint` when converted from LangGraph), `updated_at` |
| `transcript_blobs` | `hash`, `mime_type`, `size`, `data` |
