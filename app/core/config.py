from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Observabilidad
    service_name: str = "ai-ticket-triage-agents"
    service_version: str = "1.0.0"
    environment: str = "dev"
    log_level: str = "INFO"

    # Un mismo código, tres desplegables.
    #   all: orquestador + especialistas en el mismo proceso (demo)
    #   orchestrator: llama a los especialistas por HTTP (VISION_URL / LOGS_URL)
    #   vision | logs: solo ese especialista
    service_role: Literal["all", "orchestrator", "vision", "logs"] = "all"

    # Backends intercambiables. "memory"/"mock" no necesitan credenciales ni red.
    llm_backend: Literal["mock", "gemini"] = "mock"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    faq_store_name: str = ""  # File Search Store con las FAQ (RAG administrado)
    faq_path: str = "data/faqs.json"

    # Seguridad de entrada: secreto compartido con el helpdesk (cabecera X-Webhook-Token)
    webhook_token: str = "demo-webhook-token"
    # Seguridad entre servicios: token de identidad de servicio (OIDC) en las llamadas HTTP
    #   none: sin credencial | static: Bearer fijo (SERVICE_TOKEN, para compose/dev) | google: ID token OIDC
    service_auth: Literal["none", "static", "google"] = "none"
    service_token: str = ""
    # Seguridad de los especialistas cuando se exponen solos: tokens de servicio aceptados
    specialist_tokens: str = ""

    # Especialistas remotos (rol orchestrator)
    vision_url: str = ""
    logs_url: str = ""
    vision_timeout_seconds: float = 60.0
    logs_timeout_seconds: float = 45.0

    # Reglas del triaje
    new_ticket_state: str = "new"
    max_articles: int = 2          # tickets con más intercambio ya tienen un humano involucrado
    max_text_chars: int = 6000     # presupuesto de prompt
    ticket_type_enabled: bool = True  # kill switch: false = se calcula el tipo pero no se envía
    critical_threshold: int = 9
    max_images: int = 4
    max_image_bytes: int = 4_000_000

    # Logs
    logs_backend: Literal["local", "ssh"] = "local"
    logs_dir: str = "data/logs"
    logs_timezone: str = "UTC"
    logs_window_hours: int = 2
    consolidate_above: int = 10    # más errores que esto -> un diagnóstico consolidado
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key_path: str = ""
    ssh_known_hosts: str = ""      # obligatorio con logs_backend=ssh (no se aceptan claves de host nuevas)


@lru_cache
def get_settings() -> Settings:
    return Settings()
