from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        hide_input_in_errors=True,
    )

    db_host: str
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str

    ai_base_url: str = "https://api.tf.selcloud.ru/v1"
    ai_api_key: SecretStr = SecretStr("")
    ai_model: str = "glm-5.3-flash"
    ai_reasoning_effort: str = "low"
    ai_max_tokens: int = 2048
    ai_timeout_seconds: float = 60.0

    # Временно нужны старому модулю перевода.
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b-instruct"

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()