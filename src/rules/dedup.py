# register dedup rule and add the previous iterations to the code


# choose key (row, ndc, ndc and day)



# if identity has been seen, remember the identity and compare and see if any others follow it
def dedup(medicine, key:str = "row") -> tuple[tuple[Medicine,...], int]:

    if key not in deduped:
        raise ValueError("unkown key used, please use ndc or description")

    if key == "row":
        kept = tuple(dict.fromkeys(medicine))

    else:
        val1 = attrgetter("ndc") if key == "ndc" else attrgetter("ndc", "edate")
        seen: set = set()
        out = []
        for meds in medicine:
            mark = val1(meds)
            if mark not in seen:
                seen.add(mark)
                out.append(meds)

        kept = tuple(out)

    numdupes = len(medicine) - len(kept)

    return kept,numdupes 


# return a result holding kept medicines and removed(should be a count variable)
