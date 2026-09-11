# reads setings from env file and sets environment

import os
from dataclasses import dataclass

from dotenv import find_dotenv, load_dotenv

# finding env file
load_dotenv(find_dotenv(usecwd=True))

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


# builds config file that the code uses
def load() -> Config:
    return Config(
        db=get("PHARMACLI_DB"),
        workers=num("PHARMACLI_WORKERS", int),
        timeout=num("PHARMACLI_TIMEOUT", int),
        log_level=get("PHARMACLI_LOG_LEVEL").upper(),
        dispensing_fee=num("PHARMACLI_DISPENSING_FEE", float),
        model_path=get("PHARMACLI_MODEL"),
    )
