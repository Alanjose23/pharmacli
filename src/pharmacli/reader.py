# define parse report and what it entails
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pharmacli.err import HeaderMismatch, InputError
from models.medicine import Medicine
from models.rows import COLUMNS, RawRow, SkippedRow



class ParseReport:
    records: tuple[Medicine,  ...]
    skipped: tuple[SkippedRow, ...]
# accepted meds and skipped items


    # make sure to count everything
    @property
    def total(self) -> int:
        return len(self.records) + len(self.skipped)

    
# define CSV reader
class CSVR:
    def __init__(self, path: Path) -> None:
        self.path = path


    def read(self) -> ParseReport:
        try:
            stream = open(self.path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise InputError(self.path,str(exc)) from exc

        with stream:
            reader = csv.reader(stream)


            try: 
                header = next(reader)

            except StopIteration:
                return ParseReport(records=(),skipped= ())

            found = tuple(cell.strip() for cell in header)
            if found != COLUMNS:
                raise headermismatch(self.path, COLUMNS,found)

            firstcol = COLUMNS[0].casefold()
            records: list[Medicine] = []
            skipped: list[SkippedRow] = []


             #init medicines and skipped rows 
            # append reasonings as well
            for raw in reader: 
                line = reader.line_num

                if not any(cell.strip() for cell in raw):
                    skipped.append(SkippedRow(line, "blank line"))
                    continue

                if len(raw) != len(COLUMNS):
                    skipped.append(SkippedRow(line, "expected columns did not match found columns"))

                par = [cell.strip() for cell in raw]
                # if medicine is outcome put to accepted then append otherwise skip []

                if par[0].casefold()  == firstcol:
                    skipped.append(SkippedRow(line, "skipped header row"))
                    continue

                row = cast(RawRow, dict(zip(COLUMNS, par)))
                out = Medicine.arow(row, line)


                if isinstance(out, Medicine):
                    records.append(out)

                else: 
                    skipped.append(out)

        # return a parse report
        return ParseReport(records= tuple(records), skipped= tuple(skipped))
   


