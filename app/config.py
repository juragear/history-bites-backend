from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str

    WIKIPEDIA_USER_AGENT: str

    # Model provider (D16). Production runs "gemini"; "ollama" is local dev only.
    # All provider-specific vars have defaults so pydantic-settings doesn't
    # crash on boot when only one provider's vars are set.
    MODEL_PROVIDER: Literal["gemini", "ollama"] = "gemini"

    GEMINI_API_KEY: str | None = None
    GEMINI_MODEL: str = "gemini-2.5-flash"

    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "gemma4:latest"

    PROMPT_VERSION: str = "v1"

    # Bearer token for /admin/* endpoints + the review HTML page (Step 8).
    # Required — pydantic-settings will fail loudly on boot if it's missing,
    # which is what we want: there's no safe default for an admin credential.
    # Set on Railway via `railway variables --set ADMIN_TOKEN=...`.
    ADMIN_TOKEN: str

    # Firebase Cloud Messaging (Step 9, D17 + D22).
    # FIREBASE_SERVICE_ACCOUNT_JSON is the entire service account JSON file
    # contents as a single string — required, no safe default. The app fails
    # loudly on boot without it. fcm.py parses it once at first send.
    # FCM_TOPIC defaults to "daily-fact" per D17. ALERT_WEBHOOK_URL is declared
    # now for forward-compat with Step 10 (cron alerts on missing facts /
    # generation failures); Step 9 only declares it.
    FIREBASE_SERVICE_ACCOUNT_JSON: str
    FCM_TOPIC: str = "daily-fact"
    ALERT_WEBHOOK_URL: str | None = None


settings = Settings()
