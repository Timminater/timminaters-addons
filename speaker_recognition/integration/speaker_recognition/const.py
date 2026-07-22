"""Constants for the Speaker Recognition integration."""

DOMAIN = "speaker_recognition"
CONF_ENTRY_TYPE = "entry_type"
CONF_URL = "url"
CONF_TOKEN = "token"
CONF_BACKEND_URL = "backend_url"  # Legacy upstream key.
CONF_STT_ENTITY = "stt_entity"
CONF_CONVERSATION_ENTITY = "conversation_entity"
CONF_MIN_CONFIDENCE = "min_confidence"

ENTRY_TYPE_MAIN = "main"
ENTRY_TYPE_STT = "stt"
ENTRY_TYPE_CONVERSATION = "conversation"
DEFAULT_URL = "http://local-speaker-recognition:8099"
DEFAULT_MIN_CONFIDENCE = 0.70
EVENT_DETECTED = "speaker_recognition_detected"
EVENT_ENROLLMENT_COMPLETED = "speaker_recognition_enrollment_completed"
