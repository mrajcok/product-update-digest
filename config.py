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
    openrouter_summarization_model: str = "google/gemma-3-27b-it"
    openrouter_dry_run_summarization_model: str = "dummy"
    openrouter_embedding_model: str = "qwen/qwen3-embedding-8b"
    # qwen3-embedding-8b produces 4096-dim vectors; update this if you switch models
    embedding_dimensions: int = 4096

    # Ollama local server (overrides OpenRouter for summarization when set)
    ollama_base_url: str = ""
    ollama_summarization_model: str = ""
    ollama_dry_run_summarization_model: str = ""  # falls back to ollama_summarization_model

    sqlite_db_path: str = "data/product_updates.db"

    github_token: str = "dummy"
    github_repo: str = "dummy/dummy"
    github_pages_branch: str = "gh-pages"

    log_level: str = "INFO"
    max_article_age_days: int = 30
    index_page_limit: int = 10


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
