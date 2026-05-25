import logging
import logging.handlers
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    openrouter_api_key: str = "dummy"
    openrouter_summarization_model: str = "anthropic/claude-sonnet-4-5"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"

    chroma_host: str = "localhost"
    chroma_port: int = 8000
    chroma_collection_name: str = "product_updates"

    sqlite_db_path: str = "data/product_updates.db"

    github_token: str = "dummy"
    github_repo: str = "dummy/dummy"
    github_pages_branch: str = "gh-pages"

    log_level: str = "INFO"
    max_article_age_days: int = 30


settings = Settings()  # type: ignore[call-arg]


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s:%(lineno)d: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.handlers.RotatingFileHandler(
                "logs/agent.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
            ),
        ],
    )
