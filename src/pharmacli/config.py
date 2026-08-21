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
}


# if value is not possible, try to reconcile with the default values for CLI
def get(name: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or DEFAULTS[name]
def getint(name: str) -> int:
    raw = get(name)
    try:
        return int(raw)
    except ValueError:
        return int(DEFAULTS[name])


# setup config
@dataclass(frozen=True, slots=True)
class Config:
    db: str
    workers: int
    timeout: int
    log_level: str


# builds config file that the code uses
def load() -> Config:
    return Config(
        db=get("PHARMACLI_DB"),
        workers=getint("PHARMACLI_WORKERS"),
        timeout=getint("PHARMACLI_TIMEOUT"),
        log_level=get("PHARMACLI_LOG_LEVEL").upper(),
    )
