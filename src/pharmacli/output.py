# choosing between file and standard output

import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


@contextmanager
#context manager depreciated because of Iterator, changed to Generator
def open_output(path: Path | None) -> Generator[TextIO]:
    if path is None:
        yield sys.stdout
        return

    with open(path, "w", newline="", encoding="utf-8") as handle:
        yield handle
