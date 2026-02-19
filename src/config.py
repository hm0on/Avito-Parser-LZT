from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://avito:avito@localhost:5432/avito_parser",
        alias="DATABASE_URL",
    )

    # OpenAI
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")

    # 2GIS
    twogis_api_key: str = Field(default="", alias="TWOGIS_API_KEY")

    # DaData
    dadata_api_key: str = Field(default="", alias="DADATA_API_KEY")
    dadata_secret_key: str = Field(default="", alias="DADATA_SECRET_KEY")

    # ФССП
    fssp_api_token: str = Field(default="", alias="FSSP_API_TOKEN")

    # Proxy for Playwright scrapers
    # Use PROXY_FILE for multiple proxies (one per line); PROXY_URL for a single proxy.
    proxy_url: str = Field(default="", alias="PROXY_URL")
    proxy_file: str = Field(default="", alias="PROXY_FILE")

    # Stage 2 Review Enrichment
    serpapi_key: str = Field(default="", alias="SERPAPI_KEY")
    vk_access_token: str = Field(default="", alias="VK_ACCESS_TOKEN")
    enable_review_enrichment: bool = Field(default=True, alias="ENABLE_REVIEW_ENRICHMENT")

    # Pipeline thresholds
    dedup_threshold: int = Field(default=85, alias="DEDUP_THRESHOLD")
    confidence_threshold: int = Field(default=70, alias="CONFIDENCE_THRESHOLD")
    max_reviews_per_company: int = Field(default=200, alias="MAX_REVIEWS_PER_COMPANY")

    # Scheduler
    scheduler_cron: str = Field(default="0 3 * * 1", alias="SCHEDULER_CRON")

    # Search keywords
    search_keywords: list[str] = [
        "монтаж водопровода",
        "прокладка канализации",
        "установка отопления",
        "электромонтажные работы",
        "вентиляция кондиционирование",
        "газопровод монтаж",
    ]

    # Region
    region: str = "Омск"
    twogis_region_id: str = "4504222397119399"
    yandex_region_code: str = "54"


settings = Settings()
