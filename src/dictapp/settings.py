from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    db_host: str
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str

    @property
    def database_url(self) -> str:
        # async url for sqlalchemy
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )
settings = Settings()