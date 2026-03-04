# Home Assistant Add-on: LLM Proxy

This add-on packages [stevelittlefish/llm_proxy](https://github.com/stevelittlefish/llm_proxy) for Home Assistant.

It exposes an Ollama-compatible API (`/api/chat`, `/api/generate`, `/api/tags`, `/api/show`) and forwards requests to either:

- an OpenAI-compatible backend (for example `llama.cpp` server), or
- another Ollama instance.

It also logs requests and responses into a local SQLite database and offers a web UI for inspection.

## Features

- Ollama-compatible endpoint on port `11434`
- Ingress web UI inside Home Assistant
- Request/response logging to `/data/llm_proxy.db`
- Backend tool blacklist support (comma-separated)
- Optional prompt-cache flag for OpenAI-compatible backends
- Optional chat text injection

## Configuration

### Add-on options

- `backend_type`: `openai` or `ollama`
- `backend_endpoint`: backend URL
- `backend_timeout`: timeout in seconds
- `enable_cors`: enable permissive CORS middleware
- `log_messages`: log parsed message content
- `log_raw_requests`: log raw request JSON
- `log_raw_responses`: log raw response JSON
- `verbose`: enable verbose request/response middleware logs
- `tool_blacklist`: comma-separated tool names (for example `web_search,execute_code`)
- `force_prompt_cache`: force prompt caching for OpenAI backend
- `max_requests`: max rows to keep in DB (`0` disables periodic cleanup task)
- `cleanup_interval`: cleanup interval in minutes (`0` disables periodic cleanup task)
- `chat_text_injection_enabled`: enable text injection on chat requests
- `chat_text_injection_text`: text to append
- `chat_text_injection_mode`: `first`, `last`, or `system`

## Usage with Home Assistant Ollama integration

Use this add-on URL in integrations that expect an Ollama API:

- `http://<home-assistant-host-or-ip>:11434`

## Upstream

Core proxy implementation originates from:

- https://github.com/stevelittlefish/llm_proxy
