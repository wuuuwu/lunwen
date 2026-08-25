# Provider contract

The application depends on `ModelPort`, not on an OpenAI or DeepSeek response type. A provider
adapter must normalize messages, tool definitions, tool calls, usage, response identifiers, and
errors into the domain-neutral request and response models in `ports/model.py`.

The application has separate adapters for OpenAI-compatible Chat Completions and the Responses
API. OpenAI and DeepSeek Chat tasks use the former; the built-in OpenAI Responses entry and custom
Responses profiles use the latter. The two wire formats remain separate, while both normalize into
the same port models. The harness still performs local JSON Schema and Pydantic validation;
server-side strict output modes are optional optimizations and are not correctness dependencies.

Responses requests use `store=False`, do not persist remote response IDs, and keep JSON-safe
continuation items only in one reviewer coroutine. Custom Provider credentials never fall back to
environment variables; built-in Provider credentials may do so for compatibility. Built-in
endpoint and protocol values come from the fixed provider catalog and are validated for task
snapshots.

Provider adapters must never:

- execute a tool themselves;
- persist API keys or authorization headers;
- decide retries for invalid authentication or invalid requests;
- expose provider objects above the adapter layer;
- silently change a model selected by the caller.

Retryable classes are connection errors, timeouts, rate limits, and internal server errors. Invalid
requests, authentication failures, output validation failures, and rubric failures are not transport
retries.
