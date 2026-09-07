from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    parts = [p.strip() for p in (value or "").replace("\n", ",").split(",")]
    return [p for p in parts if p]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://avito:avito@db:5432/avito_parser",
        alias="DATABASE_URL",
    )

    # OpenAI
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_proxy_url: str = Field(default="", alias="OPENAI_PROXY_URL")
    openai_proxy_file: str = Field(default="", alias="OPENAI_PROXY_FILE")
    openai_quality_profile: bool = Field(default=True, alias="OPENAI_QUALITY_PROFILE")
    summarizer_model: str = Field(default="gpt-4o", alias="SUMMARIZER_MODEL")
    summarizer_max_tokens: int = Field(default=2400, alias="SUMMARIZER_MAX_TOKENS")
    summarizer_temperature: float = Field(default=0.2, alias="SUMMARIZER_TEMPERATURE")
    summarizer_workers: int = Field(default=2, alias="SUMMARIZER_WORKERS")
    summarizer_max_input_reviews: int = Field(default=80, alias="SUMMARIZER_MAX_INPUT_REVIEWS")

    # SPFA (Avito phone lookup by URL)
    spfa_api_key: str = Field(default="", alias="SPFA_API_KEY")
    spfa_phone_endpoint: str = Field(default="https://spfa.ru/api/phone/", alias="SPFA_PHONE_ENDPOINT")
    spfa_timeout_seconds: float = Field(default=30.0, alias="SPFA_TIMEOUT_SECONDS")

    # DaData (ФНС/ЕГРЮЛ/ЕГРИП)
    dadata_api_key: str = Field(default="", alias="DADATA_API_KEY")
    dadata_secret_key: str = Field(default="", alias="DADATA_SECRET_KEY")

    # Shared/default proxy settings.
    proxy_storage_dir: str = Field(default="storage/proxies", alias="PROXY_STORAGE_DIR")
    proxy_url: str = Field(default="", alias="PROXY_URL")
    proxy_file: str = Field(default="", alias="PROXY_FILE")

    # SX.org proxy API key
    sx_proxy_api_key: str = Field(default="", alias="SX_PROXY_API_KEY")

    # Per-runner proxy files/urls.
    avito_proxy_url: str = Field(default="", alias="AVITO_PROXY_URL")
    avito_proxy_file: str = Field(default="", alias="AVITO_PROXY_FILE")
    yandex_proxy_url: str = Field(default="", alias="YANDEX_PROXY_URL")
    yandex_proxy_file: str = Field(default="", alias="YANDEX_PROXY_FILE")
    twogis_proxy_url: str = Field(default="", alias="TWOGIS_PROXY_URL")
    twogis_proxy_file: str = Field(default="", alias="TWOGIS_PROXY_FILE")
    enrichment_proxy_url: str = Field(default="", alias="ENRICHMENT_PROXY_URL")
    enrichment_proxy_file: str = Field(default="", alias="ENRICHMENT_PROXY_FILE")

    # Dedicated website-scan proxy scope.
    website_proxy_url: str = Field(default="", alias="WEBSITE_PROXY_URL")
    website_proxy_file: str = Field(default="", alias="WEBSITE_PROXY_FILE")
    website_proxy_scheme: str = Field(default="", alias="WEBSITE_PROXY_SCHEME")

    # Avito browser settings
    avito_browser_name: str = Field(default="chromium", alias="AVITO_BROWSER_NAME")
    avito_browser_headless: bool = Field(default=True, alias="AVITO_BROWSER_HEADLESS")
    avito_proxy_scheme: str = Field(default="socks5", alias="AVITO_PROXY_SCHEME")
    avito_proxy_fallback_scheme: str = Field(default="", alias="AVITO_PROXY_FALLBACK_SCHEME")
    avito_proxy_max_rotations: int = Field(default=6, alias="AVITO_PROXY_MAX_ROTATIONS")
    avito_proxy_fail_cooldown_seconds: float = Field(
        default=180.0,
        alias="AVITO_PROXY_FAIL_COOLDOWN_SECONDS",
    )
    avito_request_min_interval_seconds: float = Field(
        default=4.0,
        alias="AVITO_REQUEST_MIN_INTERVAL_SECONDS",
    )
    avito_fetch_retries: int = Field(default=3, alias="AVITO_FETCH_RETRIES")
    avito_page_timeout_ms: int = Field(default=180_000, alias="AVITO_PAGE_TIMEOUT_MS")
    avito_max_pages: int = Field(default=20, alias="AVITO_MAX_PAGES")
    avito_review_pause_seconds: float = Field(default=1.0, alias="AVITO_REVIEW_PAUSE_SECONDS")
    avito_review_workers: int = Field(default=3, alias="AVITO_REVIEW_WORKERS")
    enable_avito_review_parsing: bool = Field(default=True, alias="ENABLE_AVITO_REVIEW_PARSING")
    avito_search_areas: str = Field(default="omsk,omskaya_oblast", alias="AVITO_SEARCH_AREAS")

    # Avito proxy precheck
    avito_precheck_enabled: bool = Field(default=True, alias="AVITO_PRECHECK_ENABLED")
    avito_precheck_concurrency: int = Field(default=20, alias="AVITO_PRECHECK_CONCURRENCY")
    avito_precheck_timeout_seconds: float = Field(default=10.0, alias="AVITO_PRECHECK_TIMEOUT_SECONDS")
    avito_precheck_sample_size: int = Field(default=0, alias="AVITO_PRECHECK_SAMPLE_SIZE")  # 0=all
    avito_precheck_min_usable: int = Field(default=1, alias="AVITO_PRECHECK_MIN_USABLE")
    avito_precheck_min_avito_ok: int = Field(default=1, alias="AVITO_PRECHECK_MIN_AVITO_OK")
    avito_retry_jitter_max_seconds: float = Field(default=2.0, alias="AVITO_RETRY_JITTER_MAX_SECONDS")

    # Review enrichment
    serpapi_key: str = Field(default="", alias="SERPAPI_KEY")
    enable_review_enrichment: bool = Field(default=True, alias="ENABLE_REVIEW_ENRICHMENT")
    avito_light_enrichment: bool = Field(default=True, alias="AVITO_LIGHT_ENRICHMENT")
    enable_phone_parsing: bool = Field(default=True, alias="ENABLE_PHONE_PARSING")
    enable_ddg_search: bool = Field(default=False, alias="ENABLE_DDG_SEARCH")
    enable_fssp_check: bool = Field(default=False, alias="ENABLE_FSSP_CHECK")
    fssp_processing_retry_delay_seconds: int = Field(
        default=1800,
        alias="FSSP_PROCESSING_RETRY_DELAY_SECONDS",
    )
    fssp_processing_max_retries: int = Field(
        default=4,
        alias="FSSP_PROCESSING_MAX_RETRIES",
    )

    # Pipeline thresholds
    dedup_threshold: int = Field(default=85, alias="DEDUP_THRESHOLD")
    confidence_threshold: int = Field(default=70, alias="CONFIDENCE_THRESHOLD")
    max_reviews_per_company: int = Field(default=200, alias="MAX_REVIEWS_PER_COMPANY")
    pipeline_parallel_sources: bool = Field(default=True, alias="PIPELINE_PARALLEL_SOURCES")
    pipeline_source_parallelism: int = Field(default=2, alias="PIPELINE_SOURCE_PARALLELISM")
    enrichment_workers: int = Field(default=6, alias="ENRICHMENT_WORKERS")
    ai_workers: int = Field(default=8, alias="AI_WORKERS")
    review_search_max_queries: int = Field(default=2, alias="REVIEW_SEARCH_MAX_QUERIES")
    website_scan_max_pages: int = Field(default=8, alias="WEBSITE_SCAN_MAX_PAGES")
    website_scan_max_images: int = Field(default=2, alias="WEBSITE_SCAN_MAX_IMAGES")
    website_scan_max_pdfs: int = Field(default=2, alias="WEBSITE_SCAN_MAX_PDFS")
    website_scan_timeout_seconds: float = Field(default=10.0, alias="WEBSITE_SCAN_TIMEOUT_SECONDS")
    website_scan_enable_ocr: bool = Field(default=False, alias="WEBSITE_SCAN_ENABLE_OCR")

    # Precision policy
    strict_legal_match: bool = Field(default=True, alias="STRICT_LEGAL_MATCH")
    legal_binding_memory_enabled: bool = Field(default=True, alias="LEGAL_BINDING_MEMORY_ENABLED")
    legal_name_match_min_overlap: float = Field(default=0.95, alias="LEGAL_NAME_MATCH_MIN_OVERLAP")
    legal_auto_apply_name_lookup: bool = Field(default=False, alias="LEGAL_AUTO_APPLY_NAME_LOOKUP")
    relevance_min_confidence: float = Field(default=0.75, alias="RELEVANCE_MIN_CONFIDENCE")
    relevance_model: str = Field(default="gpt-4o", alias="RELEVANCE_MODEL")
    relevance_timeout_seconds: int = Field(default=90, alias="RELEVANCE_TIMEOUT_SECONDS")
    relevance_max_attempts: int = Field(default=3, alias="RELEVANCE_MAX_ATTEMPTS")
    relevance_use_openai: bool = Field(default=True, alias="RELEVANCE_USE_OPENAI")

    # Scheduler
    scheduler_cron: str = Field(default="0 3 * * 0", alias="SCHEDULER_CRON")
    scheduler_timezone: str = Field(default="Europe/Moscow", alias="SCHEDULER_TIMEZONE")

    # Search profile (precision-first)
    search_keywords: list[str] = [
        "бурение скважин на воду",
        "скважина под ключ",
        "артезианская скважина",
        "септик под ключ",
        "автономная канализация",
        "выгребная яма из ЖБ колец",
        "колодец на воду",
        "копка колодцев",
        "горизонтально направленное бурение",
        "ГНБ Омск",
        "монтаж отопления",
        "водоочистка",
        "водопровод и канализация",
        "алмазное бурение",
    ]
    search_plumbing_keywords: str = Field(
        default=(
            "бурение скважин на воду,скважина под ключ,артезианская скважина,"
            "септик под ключ,автономная канализация,выгребная яма из ЖБ колец,"
            "колодец на воду,копка колодцев,горизонтально направленное бурение,"
            "ГНБ Омск,монтаж отопления,водоочистка,"
            "водопровод и канализация,алмазное бурение"
        ),
        alias="SEARCH_PLUMBING_KEYWORDS",
    )
    enable_adjacent_services: bool = Field(default=False, alias="ENABLE_ADJACENT_SERVICES")

    # Geo target
    target_geo_mode: str = Field(default="strict_omsk_oblast", alias="TARGET_GEO_MODE")
    target_geo_terms: str = Field(
        default=(
            "омск,омская область,калачинск,исилькуль,тюкалинск,таврическое,"
            "муромцево,марьяновка,любинский,азово,черлак,большеречье,нововаршавка,"
            "называевск,кормиловка,павлоградка,саргатское"
        ),
        alias="TARGET_GEO_TERMS",
    )

    # Region
    region: str = "Омск"
    twogis_region_id: str = "4504222397119399"
    yandex_region_code: str = ""
    twogis_proxy_scheme: str = Field(default="", alias="TWOGIS_PROXY_SCHEME")
    twogis_debug_browser: bool = Field(default=False, alias="TWOGIS_DEBUG_BROWSER")
    twogis_search_max_pages: int = Field(default=8, alias="TWOGIS_SEARCH_MAX_PAGES")
    twogis_firm_workers: int = Field(default=4, alias="TWOGIS_FIRM_WORKERS")
    twogis_tabs_per_browser: int = Field(default=3, alias="TWOGIS_TABS_PER_BROWSER")
    twogis_company_retry_attempts: int = Field(default=2, alias="TWOGIS_COMPANY_RETRY_ATTEMPTS")
    twogis_retry_until_success: bool = Field(default=True, alias="TWOGIS_RETRY_UNTIL_SUCCESS")
    twogis_max_proxy_rotations: int = Field(default=30, alias="TWOGIS_MAX_PROXY_ROTATIONS")
    twogis_search_areas: str = Field(default="Омск,Омская область", alias="TWOGIS_SEARCH_AREAS")

    # Yandex debug mode (visible browser + slower actions + pause before close)
    yandex_debug_browser: bool = Field(default=False, alias="YANDEX_DEBUG")
    yandex_debug_slow_mo_ms: int = Field(default=0, alias="YANDEX_DEBUG_SLOW_MO_MS")
    yandex_debug_hold_seconds: float = Field(default=0.0, alias="YANDEX_DEBUG_HOLD_SECONDS")
    yandex_debug_screenshot_path: str = Field(
        default="storage/yandex_debug_last.png",
        alias="YANDEX_DEBUG_SCREENSHOT_PATH",
    )
    yandex_browser_restart_attempts: int = Field(
        default=3,
        alias="YANDEX_BROWSER_RESTART_ATTEMPTS",
    )
    yandex_state_view_immediate: bool = Field(
        default=True,
        alias="YANDEX_STATE_VIEW_IMMEDIATE",
    )
    yandex_single_pass: bool = Field(
        default=True,
        alias="YANDEX_SINGLE_PASS",
    )
    yandex_reviews_max_attempts: int = Field(
        default=3,
        alias="YANDEX_REVIEWS_MAX_ATTEMPTS",
    )

    # List-org registry checker
    listorg_enabled: bool = Field(default=True, alias="LISTORG_ENABLED")
    listorg_primary: bool = Field(default=True, alias="LISTORG_PRIMARY")
    listorg_fallback_enabled: bool = Field(default=True, alias="LISTORG_FALLBACK_ENABLED")
    listorg_allow_name_lookup: bool = Field(default=False, alias="LISTORG_ALLOW_NAME_LOOKUP")
    listorg_timeout_seconds: int = Field(default=20, alias="LISTORG_TIMEOUT_SECONDS")
    listorg_rate_limit_rps: float = Field(default=1.0, alias="LISTORG_RATE_LIMIT_RPS")
    listorg_max_attempts: int = Field(default=3, alias="LISTORG_MAX_ATTEMPTS")
    listorg_use_enrichment_proxy: bool = Field(default=True, alias="LISTORG_USE_ENRICHMENT_PROXY")
    listorg_name_match_min_score: float = Field(default=0.75, alias="LISTORG_NAME_MATCH_MIN_SCORE")

    # Rusprofile checker (fallback after list-org)
    rusprofile_enabled: bool = Field(default=True, alias="RUSPROFILE_ENABLED")
    rusprofile_timeout_seconds: int = Field(default=20, alias="RUSPROFILE_TIMEOUT_SECONDS")
    rusprofile_max_attempts: int = Field(default=3, alias="RUSPROFILE_MAX_ATTEMPTS")
    rusprofile_use_enrichment_proxy: bool = Field(default=False, alias="RUSPROFILE_USE_ENRICHMENT_PROXY")
    rusprofile_allow_name_lookup: bool = Field(default=False, alias="RUSPROFILE_ALLOW_NAME_LOOKUP")
    rusprofile_name_match_min_score: float = Field(default=0.75, alias="RUSPROFILE_NAME_MATCH_MIN_SCORE")

    # OpenAI company-profile fallback (last resort)
    openai_company_fallback_enabled: bool = Field(
        default=True,
        alias="OPENAI_COMPANY_FALLBACK_ENABLED",
    )
    openai_company_fallback_model: str = Field(
        default="gpt-4o",
        alias="OPENAI_COMPANY_FALLBACK_MODEL",
    )
    openai_company_fallback_timeout_seconds: int = Field(
        default=90,
        alias="OPENAI_COMPANY_FALLBACK_TIMEOUT_SECONDS",
    )
    openai_company_fallback_max_attempts: int = Field(
        default=5,
        alias="OPENAI_COMPANY_FALLBACK_MAX_ATTEMPTS",
    )
    openai_company_fallback_use_proxy: bool = Field(
        default=True,
        alias="OPENAI_COMPANY_FALLBACK_USE_PROXY",
    )

    # 2GIS Flamp fallback for reviews
    twogis_flamp_fallback_enabled: bool = Field(default=True, alias="TWOGIS_FLAMP_FALLBACK_ENABLED")
    flamp_max_reviews_per_company: int = Field(default=15, alias="FLAMP_MAX_REVIEWS_PER_COMPANY")
    flamp_max_attempts: int = Field(default=5, alias="FLAMP_MAX_ATTEMPTS")
    flamp_timeout_seconds: float = Field(default=15.0, alias="FLAMP_TIMEOUT_SECONDS")

    @property
    def target_geo_terms_list(self) -> list[str]:
        return [term.lower() for term in _split_csv(self.target_geo_terms)]

    @property
    def avito_search_areas_list(self) -> list[str]:
        out = _split_csv(self.avito_search_areas)
        return out or ["omsk", "omskaya_oblast"]

    @property
    def twogis_search_areas_list(self) -> list[str]:
        out = _split_csv(self.twogis_search_areas)
        return out or ["Омск", "Омская область"]

    @property
    def search_keywords_effective(self) -> list[str]:
        plumbing = _split_csv(self.search_plumbing_keywords)
        if plumbing:
            return plumbing
        return list(self.search_keywords)


settings = Settings()
