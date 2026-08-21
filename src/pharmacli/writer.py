# writes Medicine records back out as a NADAC CSV

import csv
from collections.abc import Sequence
from typing import TextIO

from pharmacli.models.medicine import Medicine
from pharmacli.models.rows import COLUMNS


class CSVW:
    def __init__(self, stream: TextIO) -> None:
        self._writer = csv.writer(stream, lineterminator="\n")

    def write(self, records: Sequence[Medicine]) -> int:
        self._writer.writerow(COLUMNS)
        for record in records:
            self._writer.writerow(record.torow())
        return len(records)
