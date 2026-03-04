package config

import (
	"fmt"

	"github.com/BurntSushi/toml"
)

// Config represents the application configuration
type Config struct {
	Server            ServerConfig            `toml:"server"`
	Backend           BackendConfig           `toml:"backend"`
	BackendOpenAI     BackendOpenAIConfig     `toml:"backend_openai"`
	Database          DatabaseConfig          `toml:"database"`
	ChatTextInjection ChatTextInjectionConfig `toml:"chat_text_injection"`
}

// ServerConfig holds the server settings
type ServerConfig struct {
	Host            string `toml:"host"`
	Port            int    `toml:"port"`
	EnableCORS      bool   `toml:"enable_cors"`
	LogMessages     bool   `toml:"log_messages"`
	LogRawRequests  bool   `toml:"log_raw_requests"`
	LogRawResponses bool   `toml:"log_raw_responses"`
	Verbose         bool   `toml:"verbose"`
}

// BackendConfig holds the backend service settings
type BackendConfig struct {
	Type          string   `toml:"type"` // "openai" or "ollama"
	Endpoint      string   `toml:"endpoint"`
	Timeout       int      `toml:"timeout"`        // in seconds
	ToolBlacklist []string `toml:"tool_blacklist"` // List of tool names to filter out
}

// DatabaseConfig holds the database settings
type DatabaseConfig struct {
	Path            string `toml:"path"`
	MaxRequests     int    `toml:"max_requests"`     // Maximum number of requests to keep (0 = unlimited)
	CleanupInterval int    `toml:"cleanup_interval"` // Cleanup interval in minutes (0 = disabled)
}

// BackendOpenAIConfig holds OpenAI-specific backend settings
type BackendOpenAIConfig struct {
	ForcePromptCache bool `toml:"force_prompt_cache"` // Force prompt caching on all requests
}

// ChatTextInjectionConfig holds the chat text injection settings
type ChatTextInjectionConfig struct {
	Enabled bool   `toml:"enabled"` // Enable text injection
	Text    string `toml:"text"`    // Text to inject
	Mode    string `toml:"mode"`    // "first", "last", or "system" - which message to inject into
}

// Load reads and parses the configuration file
func Load(path string) (*Config, error) {
	config := Config{
		Server: ServerConfig{
			Host: "0.0.0.0",
			Port: 11434,
		},
		Backend: BackendConfig{
			Timeout: 300,
		},
		Database: DatabaseConfig{
			Path:            "./llm_proxy.db",
			MaxRequests:     100,
			CleanupInterval: 5,
		},
		ChatTextInjection: ChatTextInjectionConfig{
			Mode: "last",
		},
	}

	metadata, err := toml.DecodeFile(path, &config)
	if err != nil {
		return nil, fmt.Errorf("failed to read/parse config file: %w", err)
	}

	// Fail on unknown keys
	if len(metadata.Undecoded()) > 0 {
		return nil, fmt.Errorf("unknown keys in config file: %v", metadata.Undecoded())
	}

	// Validate backend type
	if config.Backend.Type != "openai" && config.Backend.Type != "ollama" {
		return nil, fmt.Errorf("invalid backend type: %s (must be 'openai' or 'ollama')", config.Backend.Type)
	}

	if config.Server.Port < 1 || config.Server.Port > 65535 {
		return nil, fmt.Errorf("invalid server.port: %d (must be 1-65535)", config.Server.Port)
	}

	if config.Backend.Timeout < 1 {
		return nil, fmt.Errorf("invalid backend.timeout: %d (must be >= 1)", config.Backend.Timeout)
	}

	if config.Database.MaxRequests < 0 {
		return nil, fmt.Errorf("invalid database.max_requests: %d (must be >= 0)", config.Database.MaxRequests)
	}

	if config.Database.CleanupInterval < 0 {
		return nil, fmt.Errorf("invalid database.cleanup_interval: %d (must be >= 0)", config.Database.CleanupInterval)
	}

	// Validate chat text injection mode
	if config.ChatTextInjection.Mode != "" && config.ChatTextInjection.Mode != "first" && config.ChatTextInjection.Mode != "last" && config.ChatTextInjection.Mode != "system" {
		return nil, fmt.Errorf("invalid chat_text_injection.mode: %s (must be 'first', 'last', or 'system')", config.ChatTextInjection.Mode)
	}

	return &config, nil
}
