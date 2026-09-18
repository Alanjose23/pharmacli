# reads setings from env file and sets environment

import os
from dataclasses import dataclass
from pathlib import Path


# .env, looked for from the working directory upwards. A real environment
# variable wins, which is what python-dotenv did before these eight lines
# replaced it.
# ponytail: no inline comments after a value; put comments on their own line
def _load_env() -> None:
    for folder in (Path.cwd(), *Path.cwd().parents):
        env = folder / ".env"
        if env.is_file():
            for line in env.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                key = key.strip().removeprefix("export ").strip()
                if key and not key.startswith("#"):
                    os.environ.setdefault(key, value.strip().strip("\"'"))
            return


_load_env()

#  default settings
DEFAULTS = {
    "PHARMACLI_DB": "pharmacli.db",
    "PHARMACLI_WORKERS": "10",
    "PHARMACLI_TIMEOUT": "30",
    "PHARMACLI_LOG_LEVEL": "INFO",
    # the model `pharmacli sweep` promotes. Its .json sidecar sits beside it.
    "PHARMACLI_MODEL": "models/pharmacli-mover.joblib",
    # state fee for prescription on top of ingredient cost, 10.64 is Medicaid standard
    "PHARMACLI_DISPENSING_FEE": "10.64",
    # used for margin math
    # week 9: RAG. Chroma and Ollama run in docker compose, published on these
    # localhost ports. The model tags were checked against Ollama's library.
    "PHARMACLI_CHROMA_URL": "http://127.0.0.1:8001",
    "PHARMACLI_OLLAMA_URL": "http://127.0.0.1:11434",
    "PHARMACLI_LLM": "qwen2.5:3b-instruct",
    "PHARMACLI_EMBED": "all-minilm:l6-v2",
    # fixed | section: which chunk collection answers. The eval compares both.
    "PHARMACLI_CHUNKING": "section",
    "PHARMACLI_K": "3",
    # below this cosine similarity the best passage is too far off to answer from
    "PHARMACLI_RELEVANCE_FLOOR": "0.35",
}


# if value is not possible, try to reconcile with the default values for CLI
def get(name: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or DEFAULTS[name]
# parser for numerics, env values fall back on default if env is not setup properly
def num(name: str, cast):
    try:
        return cast(get(name))
    except ValueError:
        return cast(DEFAULTS[name])


# setup config
@dataclass(frozen=True, slots=True)
class Config:
    db: str
    workers: int
    timeout: int
    log_level: str
    dispensing_fee: float
    model_path: str
    chroma_url: str
    ollama_url: str
    llm: str
    embed_model: str
    chunking: str
    k: int
    relevance_floor: float


# builds config file that the code uses
def load() -> Config:
    return Config(
        db=get("PHARMACLI_DB"),
        workers=num("PHARMACLI_WORKERS", int),
        timeout=num("PHARMACLI_TIMEOUT", int),
        log_level=get("PHARMACLI_LOG_LEVEL").upper(),
        dispensing_fee=num("PHARMACLI_DISPENSING_FEE", float),
        model_path=get("PHARMACLI_MODEL"),
        chroma_url=get("PHARMACLI_CHROMA_URL"),
        ollama_url=get("PHARMACLI_OLLAMA_URL"),
        llm=get("PHARMACLI_LLM"),
        embed_model=get("PHARMACLI_EMBED"),
        chunking=get("PHARMACLI_CHUNKING"),
        k=num("PHARMACLI_K", int),
        relevance_floor=num("PHARMACLI_RELEVANCE_FLOOR", float),
    )
