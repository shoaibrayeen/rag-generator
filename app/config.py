"""
Single home for every tunable in the application.

Every field on `Settings` has a default and can be overridden by an environment
variable of the same name (or a line in `.env`). Nothing tunable is hard-coded
anywhere else: other modules import `settings` and read from it.

To go live you only need to set LLM_API_URL, LLM_API_KEY and LLM_MODEL in `.env`.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Generation LLM (OpenAI-compatible chat/completions endpoint) ---
    LLM_API_URL: str = "https://api.openai.com/v1"  # base URL; "/chat/completions" is appended
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_TOKENS: int = 1024
    LLM_TIMEOUT_S: int = 60

    # --- Embeddings (fastembed) ---
    EMBEDDING_MODEL: str = "BAAI/bge-small-en-v1.5"
    EMBEDDING_BATCH_SIZE: int = 64

    # --- Chunking (overlapping, metadata-tagged) ---
    CHUNK_SIZE_CHARS: int = 1200
    CHUNK_OVERLAP_CHARS: int = 200
    CSV_ROWS_PER_PAGE: int = 50  # a CSV "page" is a block of this many rows

    # --- OCR fallback (Tesseract) ---
    OCR_ENABLED: bool = True
    OCR_MIN_CHARS_PER_PAGE: int = 20  # a PDF page with fewer extractable chars is OCR'd
    OCR_LANG: str = "eng"
    OCR_DPI: int = 200

    # --- Retrieval (hybrid: dense + BM25, fused with RRF) ---
    DENSE_TOP_K: int = 20
    BM25_TOP_K: int = 20
    FUSED_TOP_K: int = 6  # chunks handed to the LLM
    RRF_K: int = 60

    # --- Storage / server ---
    DATA_DIR: Path = Path("data")
    DEFAULT_COLLECTION: str = "default"
    MAX_UPLOAD_MB: int = 50
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # --- Evaluation (Claude as judge) ---
    ANTHROPIC_API_KEY: str = ""
    JUDGE_MODEL: str = "claude-opus-5"
    JUDGE_MAX_TOKENS: int = 4096
    EVAL_MIN_FAITHFULNESS: float = 3.5
    RAG_API_URL: str = "http://localhost:8000"  # used by the CLI and the eval script

    # Derived paths
    @property
    def uploads_dir(self) -> Path:
        return self.DATA_DIR / "uploads"

    @property
    def chroma_dir(self) -> Path:
        return self.DATA_DIR / "chroma"

    @property
    def documents_file(self) -> Path:
        return self.DATA_DIR / "documents.json"

    @property
    def chat_completions_url(self) -> str:
        return self.LLM_API_URL.rstrip("/") + "/chat/completions"


# Extension -> extractor name. Edit this dict to add or remove supported formats;
# each value must match a key in `app.ingest.extractors.EXTRACTORS`.
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".html": "html",
    ".htm": "html",
    ".txt": "text",
    ".md": "text",
    ".csv": "csv",
    # Image inputs go through Tesseract OCR
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tiff": "image",
    ".tif": "image",
}

settings = Settings()
