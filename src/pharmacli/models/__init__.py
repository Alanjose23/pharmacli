# Intentionally empty -- package marker only.
#
# REMOVED: a 26-line re-export barrel (Drug, Medicine, Price, PriceRes, DATES,
# COLUMNS, REASON, RawRow, SkippedRow, parsedate, parseotc, parseprice) plus its
# __all__. Nothing ever imported `from pharmacli.models import ...` -- every
# caller reaches for models.medicine / models.rows directly, and the top-level
# pharmacli/__init__.py already re-exports the same names from those modules.
# It was a second barrel duplicating the first, serving no importer.
