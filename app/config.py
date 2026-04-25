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

    # Step 10 cron thresholds.
    # REVIEW_QUEUE_TARGET (Backend Architecture): how many pending_review rows
    # the every-6h generation cron tops up to. Below this, run_generation calls
    # generate_one_pool_fact in a loop until target is met or generation gives
    # up. 20 keeps Will with a meaningful queue but caps Gemini spend.
    # APPROVED_ALERT_THRESHOLD (D8): if approved pool count drops below this
    # at the end of a generation cron, send_alert fires so Will knows to review
    # more. 3 = roughly one push-day of buffer.
    REVIEW_QUEUE_TARGET: int = 20
    APPROVED_ALERT_THRESHOLD: int = 3

    # Step 12: CORS allowlist for any future browser-based admin/dashboard
    # client. Comma-separated origins; "*" allows any. Default is "*" for
    # local dev convenience — tighten in production by setting
    # CORS_ORIGINS=https://your.domain,http://localhost:3000 on Railway.
    # `allow_credentials=False` is wired in main.py so the wildcard actually
    # works (browsers reject `*` + credentials=true).
    CORS_ORIGINS: str = "*"


settings = Settings()
