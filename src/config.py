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

    # DaData (ФНС/ЕГРЮЛ/ЕГРИП)
    dadata_api_key: str = Field(default="", alias="DADATA_API_KEY")
    dadata_secret_key: str = Field(default="", alias="DADATA_SECRET_KEY")

    # Proxy for Playwright scrapers
    # Use PROXY_FILE for multiple proxies (one per line); PROXY_URL for a single proxy.
    proxy_url: str = Field(default="", alias="PROXY_URL")
    proxy_file: str = Field(default="", alias="PROXY_FILE")

    # SX.org proxy (RU для скрапинга, US для OpenAI API)
    sx_proxy_api_key: str = Field(default="", alias="SX_PROXY_API_KEY")
    sx_proxy_port_id_ru: int = Field(default=0, alias="SX_PROXY_PORT_ID_RU")
    sx_proxy_port_id_us: int = Field(default=0, alias="SX_PROXY_PORT_ID_US")
    # Safety: forbid автоматические покупки портов на проде
    sx_proxy_allow_create: bool = Field(default=False, alias="SX_PROXY_ALLOW_CREATE")

    # Avito cookies via spfa.ru (~12₽/set, lasts 12h) — bypass Avito firewall
    # Register at https://spfa.ru to get a key
    avito_cookies_api_key: str = Field(default="", alias="AVITO_COOKIES_API_KEY")

    # Stage 2 Review Enrichment
    serpapi_key: str = Field(default="", alias="SERPAPI_KEY")
    enable_review_enrichment: bool = Field(default=True, alias="ENABLE_REVIEW_ENRICHMENT")
    enable_phone_parsing: bool = Field(default=True, alias="ENABLE_PHONE_PARSING")

    # Pipeline thresholds
    dedup_threshold: int = Field(default=85, alias="DEDUP_THRESHOLD")
    confidence_threshold: int = Field(default=70, alias="CONFIDENCE_THRESHOLD")
    max_reviews_per_company: int = Field(default=200, alias="MAX_REVIEWS_PER_COMPANY")

    # Concurrency (pipeline parallelism)
    enrich_concurrency: int = Field(default=5, alias="ENRICH_CONCURRENCY")
    ai_concurrency: int = Field(default=5, alias="AI_CONCURRENCY")
    collector_max_keywords: int = Field(default=3, alias="COLLECTOR_MAX_KEYWORDS")
    playwright_max_keywords: int = Field(default=2, alias="PLAYWRIGHT_MAX_KEYWORDS")
    avito_max_keywords: int = Field(default=20, alias="AVITO_MAX_KEYWORDS")
    yandex_max_keywords: int = Field(default=20, alias="YANDEX_MAX_KEYWORDS")
    twogis_max_keywords: int = Field(default=20, alias="TWOGIS_MAX_KEYWORDS")

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

    # 2GIS
    twogis_api_key: str = Field(default="", alias="TWOGIS_API_KEY")
    twogis_debug_browser: bool = Field(default=False, alias="TWOGIS_DEBUG_BROWSER")
    twogis_search_max_pages: int = Field(default=8, alias="TWOGIS_SEARCH_MAX_PAGES")
    twogis_firm_workers: int = Field(default=4, alias="TWOGIS_FIRM_WORKERS")
    twogis_tabs_per_browser: int = Field(default=3, alias="TWOGIS_TABS_PER_BROWSER")
    twogis_company_retry_attempts: int = Field(default=2, alias="TWOGIS_COMPANY_RETRY_ATTEMPTS")
    twogis_retry_until_success: bool = Field(default=True, alias="TWOGIS_RETRY_UNTIL_SUCCESS")
    twogis_max_proxy_rotations: int = Field(default=30, alias="TWOGIS_MAX_PROXY_ROTATIONS")
    # Искать по всей Омской области (+ районные города), а не только по Омску
    twogis_search_region: bool = Field(default=True, alias="TWOGIS_SEARCH_REGION")

    # Yandex debug mode
    yandex_debug_browser: bool = Field(default=False, alias="YANDEX_DEBUG")
    yandex_debug_slow_mo_ms: int = Field(default=0, alias="YANDEX_DEBUG_SLOW_MO_MS")
    yandex_debug_hold_seconds: float = Field(default=0.0, alias="YANDEX_DEBUG_HOLD_SECONDS")
    yandex_debug_screenshot_path: str = Field(
        default="storage/yandex_debug_last.png",
        alias="YANDEX_DEBUG_SCREENSHOT_PATH",
    )
    yandex_browser_restart_attempts: int = Field(default=3, alias="YANDEX_BROWSER_RESTART_ATTEMPTS")
    yandex_state_view_immediate: bool = Field(default=True, alias="YANDEX_STATE_VIEW_IMMEDIATE")
    yandex_single_pass: bool = Field(default=True, alias="YANDEX_SINGLE_PASS")

    # Region
    region: str = "Омск"
    twogis_region_id: str = "4504222397119399"
    # Leave empty to use global /maps/ URL (more stable)
    yandex_region_code: str = ""


settings = Settings()
