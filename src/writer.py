# import models folder
import csv
from collections.abc import Sequence
from typing import TextIO

from .models.medicine import Medicine
from .models.rows import COLUMNS


# define a csv writer over an open filestream
class CSVW:

## should be the same ruleset
    def __init__(self, stream: TextIO) -> None:
        self._writer = csv.writer(stream, lineterminator="\n")
# write column names

# write expected medicines
    # write as one lines
    def write(self, records:Sequence[Medicine]) -> int:
        self._writer.writerow(COLUMNS)
        for record in records:
            self._writer.writerow(record.torow())

# return number of medicines written
        return len(records)