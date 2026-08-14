# choosing between file and standard output

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


@contextmanager
def open_output(path: Path | None) -> Iterator[TextIO]:
    if path is None:
        yield sys.stdout
        return

    handle = open(path, "w", newline="", encoding="utf-8")
    try:
        yield handle 

    finally:
        handle.close()