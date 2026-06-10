"""Configuration helpers for the Cyborg service."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8420
DEFAULT_POOL_SIZE = 4
DEFAULT_ENV_FILE_NAME = ".env"
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default.expanduser()


def _load_cyborg_env_files() -> None:
    """Load `.env` files into the process environment without overriding explicit env vars.

    Precedence:
    1. Existing process environment
    2. `CYBORG_ENV_FILE`, if set
    3. `.env` in the current working directory
    4. `.env` in the resolved Cyborg config directory
    """

    candidates: list[Path] = []
    explicit_env_file = os.getenv("CYBORG_ENV_FILE")
    if explicit_env_file:
        candidates.append(Path(explicit_env_file).expanduser())

    candidates.append(Path.cwd() / DEFAULT_ENV_FILE_NAME)

    for path in candidates:
        _load_env_file(path)

    config_dir = _env_path("CYBORG_CONFIG_DIR", Path("~/.config/cyborg"))
    _load_env_file(config_dir / DEFAULT_ENV_FILE_NAME)


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs from a `.env` file."""

    if not path.exists() or not path.is_file():
        return

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = _parse_env_line(line, path=path, line_number=line_number)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def _parse_env_line(line: str, *, path: Path, line_number: int) -> tuple[str, str] | None:
    """Parse a single dotenv line.

    Supports:
    - blank lines and comments
    - optional `export KEY=...`
    - single-quoted values
    - double-quoted values with standard escape decoding
    - unquoted values with trailing inline comments
    """

    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    if "=" not in stripped:
        raise ValueError(f"Invalid dotenv entry in {path}:{line_number}")

    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not ENV_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"Invalid dotenv key '{key}' in {path}:{line_number}")

    value = raw_value.strip()
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            raise ValueError(f"Unterminated double-quoted value in {path}:{line_number}")
        value = bytes(value[1:-1], "utf-8").decode("unicode_escape")
    elif value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ValueError(f"Unterminated single-quoted value in {path}:{line_number}")
        value = value[1:-1]
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()

    return key, os.path.expandvars(value)


@dataclass(slots=True)
class WebhookConfig:
    """Configuration for a webhook endpoint."""
    
    url: str
    events: list[str] = field(default_factory=list)
    secret: str = ""
    retry_count: int = 3
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WebhookConfig":
        """Create from dictionary."""
        return cls(
            url=data.get("url", ""),
            events=data.get("events", []),
            secret=data.get("secret", ""),
            retry_count=data.get("retry_count", 3),
        )


@dataclass(slots=True)
class AgentMailSettings:
    """Configuration for AgentMail email provider."""

    base_url: str = "https://api.agentmail.to"
    api_key: str = ""
    default_inbox_id: str = ""
    poll_interval_seconds: float = 30.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(slots=True)
class VoiceSettings:
    """Configuration for the voice chat subsystem."""

    enabled: bool = True
    stt_model: str = "large-v3-turbo"
    stt_device: str = "cuda"
    stt_compute_type: str = "int8"
    tts_num_steps: int = 16
    voices_dir: Path = Path(__file__).parent / "voice_data" / "voices"
    lessons_dir: Path | None = None
    frontend_dir: Path | None = None
    session_max_age_days: int = 30


@dataclass(slots=True)
class PhoneSettings:
    """Configuration for the phone/telephony subsystem (Twilio)."""

    enabled: bool = False
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    base_url: str = ""
    silence_threshold: float = 0.01
    silence_duration: float = 1.5
    call_recording_enabled: bool = True
    call_recording_max_age_days: int = 30


@dataclass(slots=True)
class OpenAISettings:
    """Configuration for direct OpenAI LLM API access."""

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    default_model: str = "gpt-5.4-mini"
    memory_model: str = ""
    timeout_seconds: float = 120.0
    web_search_enabled: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def get_memory_model(self) -> str:
        return self.memory_model or self.default_model


@dataclass(slots=True)
class HarnessSettings:
    """Configuration for the local LLM harness for voice/phone."""

    enabled: bool = False
    workspace_dir: Path = Path("~/.config/cyborg/harness")
    default_model: str = "gpt-5.4-mini"
    max_history_messages: int = 20
    skill_dev_enabled: bool = False
    skill_dev_model: str = "sonnet"
    skill_dev_max_budget_usd: float = 5.0
    skill_dev_timeout_seconds: float = 300.0
    local_subagent_model: str = "gpt-5.5"


@dataclass(slots=True)
class WhatsAppBridgeSettings:
    """Configuration for the WhatsApp bridge companion service."""

    enabled: bool = False
    url: str = "ws://127.0.0.1:8430/ws"
    token: str = ""
    reconnect_interval_seconds: float = 10.0
    media_dir: Path = Path("~/.local/share/cyborg/whatsappbridge/media")


@dataclass(slots=True)
class PatienceSettings:
    """Configuration for the patience dispatch system."""

    enabled: bool = False
    model: str = "gpt-5.4-mini"
    bot_name: str = "Bot"
    max_pending_items: int = 20
    max_context_messages: int = 10


@dataclass(slots=True)
class Settings:
    """Runtime settings for the API service and CLI."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    data_dir: Path = Path("~/.local/share/cyborg")
    config_dir: Path = Path("~/.config/cyborg")
    db_path: Path | None = None
    log_path: Path | None = None
    log_level: str = "info"
    debug: bool = False
    version: str = "0.2.0"  # Application version
    pool_size: int = DEFAULT_POOL_SIZE
    webhooks: dict[str, WebhookConfig] = field(default_factory=dict)
    agentmail: AgentMailSettings = field(default_factory=AgentMailSettings)
    email_polling_enabled: bool = True
    voice: VoiceSettings = field(default_factory=VoiceSettings)
    phone: PhoneSettings = field(default_factory=PhoneSettings)
    openai: OpenAISettings = field(default_factory=OpenAISettings)
    harness: HarnessSettings = field(default_factory=HarnessSettings)
    whatsapp_bridge: WhatsAppBridgeSettings = field(default_factory=WhatsAppBridgeSettings)
    patience: PatienceSettings = field(default_factory=PatienceSettings)
    heartbeat_interval_seconds: float = 60.0
    public_url: str = ""  # Public URL for callbacks (e.g., http://localhost:8420)
    dashboard_secret: str = ""  # Shared secret for dashboard-only operations
    session_summary_idle_minutes: float = 5.0

    @property
    def dashboard_secret_configured(self) -> bool:
        return bool(self.dashboard_secret.strip())

    def __post_init__(self) -> None:
        self.data_dir = self.data_dir.expanduser()
        self.config_dir = self.config_dir.expanduser()
        if self.db_path is None:
            self.db_path = self.data_dir / "cyborg.db"
        else:
            self.db_path = self.db_path.expanduser()
        if self.log_path is not None:
            self.log_path = self.log_path.expanduser()

    @property
    def resolved_public_url(self) -> str:
        """Get the public URL, falling back to host:port if not set."""
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables."""

        _load_cyborg_env_files()
        data_dir = _env_path("CYBORG_DATA_DIR", Path("~/.local/share/cyborg"))
        config_dir = _env_path("CYBORG_CONFIG_DIR", Path("~/.config/cyborg"))
        db_path_value = os.getenv("CYBORG_DB_PATH")
        db_path = Path(db_path_value).expanduser() if db_path_value else data_dir / "cyborg.db"
        host = os.getenv("CYBORG_HOST", DEFAULT_HOST)
        port = int(os.getenv("CYBORG_PORT", str(DEFAULT_PORT)))
        pool_size = int(os.getenv("CYBORG_DB_POOL_SIZE", str(DEFAULT_POOL_SIZE)))
        log_level = os.getenv("CYBORG_LOG_LEVEL", "info")
        heartbeat_interval_seconds = float(
            os.getenv("CYBORG_HEARTBEAT_INTERVAL_SECONDS", "60")
        )

        # Logging settings
        log_path_value = os.getenv("CYBORG_LOG_PATH")
        log_path = Path(log_path_value).expanduser() if log_path_value else None
        debug = os.getenv("CYBORG_DEBUG", "").lower() in ("true", "1", "yes", "on")

        # Parse webhook configuration from environment
        webhooks: dict[str, WebhookConfig] = {}
        
        # CYBORG_WEBHOOK_EXAMPLE_URL=http://127.0.0.1:8080/webhook/cyborg
        # CYBORG_WEBHOOK_EXAMPLE_SECRET=secret
        # CYBORG_WEBHOOK_EXAMPLE_EVENTS=message.created,message.failed
        webhook_prefix = "CYBORG_WEBHOOK_"
        webhook_configs: dict[str, dict[str, Any]] = {}
        
        for key, value in os.environ.items():
            if key.startswith(webhook_prefix):
                # Parse CYBORG_WEBHOOK_{NAME}_{SETTING}
                parts = key[len(webhook_prefix):].lower().split("_")
                if len(parts) >= 2:
                    name = parts[0]
                    setting = "_".join(parts[1:])
                    if name not in webhook_configs:
                        webhook_configs[name] = {}
                    webhook_configs[name][setting] = value
        
        for name, config_data in webhook_configs.items():
            events_str = config_data.get("events", "")
            events = [e.strip() for e in events_str.split(",") if e.strip()]
            webhooks[name] = WebhookConfig(
                url=config_data.get("url", ""),
                events=events,
                secret=config_data.get("secret", ""),
                retry_count=int(config_data.get("retry_count", "3")),
            )

        public_url = os.getenv("CYBORG_PUBLIC_URL", "")
        dashboard_secret = os.getenv("CYBORG_DASHBOARD_SECRET", "")

        agentmail = AgentMailSettings(
            base_url=os.getenv("CYBORG_AGENTMAIL_BASE_URL", "https://api.agentmail.to").rstrip("/"),
            api_key=os.getenv("CYBORG_AGENTMAIL_API_KEY", ""),
            default_inbox_id=os.getenv("CYBORG_AGENTMAIL_DEFAULT_INBOX_ID", ""),
            poll_interval_seconds=float(os.getenv("CYBORG_AGENTMAIL_POLL_INTERVAL_SECONDS", "30")),
        )
        email_polling_enabled = os.getenv("CYBORG_EMAIL_POLLING_ENABLED", "true").lower() in ("true", "1", "yes", "on")

        voice = VoiceSettings(
            enabled=os.getenv("CYBORG_VOICE_ENABLED", "true").lower() not in ("false", "0", "no", "off"),
            stt_model=os.getenv("CYBORG_VOICE_STT_MODEL", "large-v3-turbo"),
            stt_device=os.getenv("CYBORG_VOICE_STT_DEVICE", "cuda"),
            stt_compute_type=os.getenv("CYBORG_VOICE_STT_COMPUTE_TYPE", "int8"),
            tts_num_steps=int(os.getenv("CYBORG_VOICE_TTS_NUM_STEPS", "16")),
            voices_dir=_env_path("CYBORG_VOICE_VOICES_DIR", Path.home() / ".cyborg" / "voices"),
            lessons_dir=Path(v).expanduser() if (v := os.getenv("CYBORG_VOICE_LESSONS_DIR")) else None,
            frontend_dir=Path(v).expanduser() if (v := os.getenv("CYBORG_VOICE_FRONTEND_DIR")) else None,
            session_max_age_days=int(os.getenv("CYBORG_VOICE_SESSION_MAX_AGE_DAYS", "30")),
        )

        session_summary_idle_minutes = float(
            os.getenv("CYBORG_SESSION_SUMMARY_IDLE_MINUTES", "5.0")
        )

        phone = PhoneSettings(
            enabled=os.getenv("CYBORG_PHONE_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            twilio_account_sid=os.getenv("CYBORG_PHONE_TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=os.getenv("CYBORG_PHONE_TWILIO_AUTH_TOKEN", ""),
            twilio_phone_number=os.getenv("CYBORG_PHONE_TWILIO_PHONE_NUMBER", ""),
            base_url=os.getenv("CYBORG_PHONE_BASE_URL", ""),
            silence_threshold=float(os.getenv("CYBORG_PHONE_SILENCE_THRESHOLD", "0.01")),
            silence_duration=float(os.getenv("CYBORG_PHONE_SILENCE_DURATION", "1.5")),
            call_recording_enabled=os.getenv("CYBORG_PHONE_CALL_RECORDING_ENABLED", "true").lower() in ("true", "1", "yes", "on"),
            call_recording_max_age_days=int(os.getenv("CYBORG_PHONE_CALL_RECORDING_MAX_AGE_DAYS", "30")),
        )

        openai_llm = OpenAISettings(
            api_key=os.getenv("CYBORG_OPENAI_API_KEY", ""),
            base_url=os.getenv("CYBORG_OPENAI_BASE_URL", "https://api.openai.com/v1"),
            default_model=os.getenv("CYBORG_OPENAI_DEFAULT_MODEL", "gpt-5.4-mini"),
            memory_model=os.getenv("CYBORG_OPENAI_MEMORY_MODEL", ""),
            timeout_seconds=float(os.getenv("CYBORG_OPENAI_TIMEOUT_SECONDS", "120")),
            web_search_enabled=os.getenv("CYBORG_OPENAI_WEB_SEARCH", "").lower() in ("1", "true", "yes"),
        )

        harness = HarnessSettings(
            enabled=os.getenv("CYBORG_HARNESS_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            workspace_dir=_env_path("CYBORG_HARNESS_WORKSPACE_DIR", Path("~/.config/cyborg/harness")),
            default_model=os.getenv("CYBORG_HARNESS_DEFAULT_MODEL", "gpt-5.4-mini"),
            max_history_messages=int(os.getenv("CYBORG_HARNESS_MAX_HISTORY_MESSAGES", "20")),
            skill_dev_enabled=os.getenv("CYBORG_HARNESS_SKILL_DEV_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            skill_dev_model=os.getenv("CYBORG_HARNESS_SKILL_DEV_MODEL", "sonnet"),
            skill_dev_max_budget_usd=float(os.getenv("CYBORG_HARNESS_SKILL_DEV_MAX_BUDGET_USD", "5.0")),
            skill_dev_timeout_seconds=float(os.getenv("CYBORG_HARNESS_SKILL_DEV_TIMEOUT_SECONDS", "300")),
            local_subagent_model=os.getenv("CYBORG_HARNESS_LOCAL_SUBAGENT_MODEL", "gpt-5.5"),
        )

        whatsapp_bridge = WhatsAppBridgeSettings(
            enabled=os.getenv("CYBORG_WHATSAPP_BRIDGE_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            url=os.getenv("CYBORG_WHATSAPP_BRIDGE_URL", "ws://127.0.0.1:8430/ws"),
            token=os.getenv("CYBORG_WHATSAPP_BRIDGE_TOKEN", ""),
            reconnect_interval_seconds=float(os.getenv("CYBORG_WHATSAPP_BRIDGE_RECONNECT_INTERVAL_SECONDS", "10")),
            media_dir=_env_path("CYBORG_WHATSAPP_BRIDGE_MEDIA_DIR", Path("~/.local/share/cyborg/whatsappbridge/media")),
        )

        patience = PatienceSettings(
            enabled=os.getenv("CYBORG_PATIENCE_ENABLED", "false").lower() in ("true", "1", "yes", "on"),
            model=os.getenv("CYBORG_PATIENCE_MODEL", "gpt-5.4-mini"),
            bot_name=os.getenv("CYBORG_SELF_NAME", "Bob"),
            max_pending_items=int(os.getenv("CYBORG_PATIENCE_MAX_PENDING", "20")),
            max_context_messages=int(os.getenv("CYBORG_PATIENCE_MAX_CONTEXT", "10")),
        )

        return cls(
            host=host,
            port=port,
            data_dir=data_dir,
            config_dir=config_dir,
            db_path=db_path,
            log_path=log_path,
            log_level=log_level,
            debug=debug,
            pool_size=pool_size,
            webhooks=webhooks,
            agentmail=agentmail,
            email_polling_enabled=email_polling_enabled,
            voice=voice,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            public_url=public_url,
            dashboard_secret=dashboard_secret,
            session_summary_idle_minutes=session_summary_idle_minutes,
            phone=phone,
            openai=openai_llm,
            harness=harness,
            whatsapp_bridge=whatsapp_bridge,
            patience=patience,
        )

    def ensure_directories(self) -> None:
        """Create the configured data and config directories."""

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if self.phone.enabled:
            (self.data_dir / "calls").mkdir(parents=True, exist_ok=True)
