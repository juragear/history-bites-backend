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


settings = Settings()
