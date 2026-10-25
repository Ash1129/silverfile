"""Runtime configuration, read from the environment (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv() -> None:
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    intake_model: str
    extraction_model: str
    review_model: str
    data_dir: Path
    taxmath_direct: bool

    @property
    def offline(self) -> bool:
        return not self.api_key

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def index_db(self) -> Path:
        return self.data_dir / "returns.sqlite"


def settings() -> Settings:
    data_dir = Path(os.environ.get("SILVERFILE_DATA_DIR", "data/runtime"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "uploads").mkdir(parents=True, exist_ok=True)
    return Settings(
        api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        intake_model=os.environ.get("SILVERFILE_INTAKE_MODEL", "claude-haiku-5-5"),
        extraction_model=os.environ.get("SILVERFILE_EXTRACTION_MODEL", "claude-sonnet-5-5"),
        review_model=os.environ.get("SILVERFILE_REVIEW_MODEL", "claude-sonnet-5-5"),
        data_dir=data_dir,
        taxmath_direct=os.environ.get("SILVERFILE_TAXMATH_DIRECT", "0") == "1",
    )
