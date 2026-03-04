#!/usr/bin/with-contenv bashio
set -euo pipefail

CONFIG_PATH="/data/config.toml"
DB_PATH="/data/llm_proxy.db"

toml_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

normalize_nullable_string() {
    if [ "${1}" = "null" ]; then
        printf ''
    else
        printf '%s' "${1}"
    fi
}

sanitize_backend_type() {
    case "$1" in
        openai|ollama)
            printf '%s' "$1"
            ;;
        *)
            bashio::log.warning "Invalid backend_type \"$1\". Falling back to \"openai\"."
            printf 'openai'
            ;;
    esac
}

sanitize_injection_mode() {
    case "$1" in
        first|last|system)
            printf '%s' "$1"
            ;;
        *)
            bashio::log.warning "Invalid chat_text_injection_mode \"$1\". Falling back to \"last\"."
            printf 'last'
            ;;
    esac
}

build_tool_blacklist_toml() {
    local csv="$1"
    local output=""
    local value=""
    local escaped=""
    IFS=',' read -ra values <<< "$csv"

    for value in "${values[@]}"; do
        value="$(printf '%s' "$value" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')"
        if [ -z "$value" ]; then
            continue
        fi

        escaped="$(toml_escape "$value")"
        if [ -n "$output" ]; then
            output="${output}, "
        fi
        output="${output}\"${escaped}\""
    done

    printf '%s' "$output"
}

BACKEND_TYPE="$(sanitize_backend_type "$(bashio::config 'backend_type')")"
BACKEND_ENDPOINT="$(normalize_nullable_string "$(bashio::config 'backend_endpoint')")"
INJECTION_MODE="$(sanitize_injection_mode "$(bashio::config 'chat_text_injection_mode')")"
INJECTION_TEXT="$(normalize_nullable_string "$(bashio::config 'chat_text_injection_text')")"
TOOL_BLACKLIST_TOML="$(build_tool_blacklist_toml "$(normalize_nullable_string "$(bashio::config 'tool_blacklist')")")"

if [ -z "${BACKEND_ENDPOINT}" ]; then
    bashio::log.warning "backend_endpoint is empty. Falling back to http://localhost:8008."
    BACKEND_ENDPOINT="http://localhost:8008"
fi

mkdir -p /data

cat > "${CONFIG_PATH}" <<EOF
[server]
host = "0.0.0.0"
port = 11434
enable_cors = $(bashio::config 'enable_cors')
log_messages = $(bashio::config 'log_messages')
log_raw_requests = $(bashio::config 'log_raw_requests')
log_raw_responses = $(bashio::config 'log_raw_responses')
verbose = $(bashio::config 'verbose')

[backend]
type = "$(toml_escape "${BACKEND_TYPE}")"
endpoint = "$(toml_escape "${BACKEND_ENDPOINT}")"
timeout = $(bashio::config 'backend_timeout')
tool_blacklist = [${TOOL_BLACKLIST_TOML}]

[backend_openai]
force_prompt_cache = $(bashio::config 'force_prompt_cache')

[database]
path = "${DB_PATH}"
max_requests = $(bashio::config 'max_requests')
cleanup_interval = $(bashio::config 'cleanup_interval')

[chat_text_injection]
enabled = $(bashio::config 'chat_text_injection_enabled')
text = "$(toml_escape "${INJECTION_TEXT}")"
mode = "$(toml_escape "${INJECTION_MODE}")"
EOF

bashio::log.info "Starting LLM Proxy"
bashio::log.info "Backend type: ${BACKEND_TYPE}"
bashio::log.info "Backend endpoint: ${BACKEND_ENDPOINT}"

exec /usr/bin/llm_proxy -config "${CONFIG_PATH}"
