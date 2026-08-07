import argparse
import sys
import warnings
from contextlib import contextmanager

import clean

VERSION = "1.0"



#add an object to close the writer
@contextmanager ###
def outfile(path: str | None):

    if path is None:
        yield sys.stdout
    else:
        handle = open(path, "w", newline = "", encoding = "utf-8")

        try:
            yield handle
        finally:
            handle.close()




#add an error handler object to fix certain functions
def errr(message: str) -> None:
    print(message, file = sys.stderr)



#command steps for dry run
def report(args) -> tuple[clean.Medicine, ...] | None:

    try:
        medicine, dupes = clean.orgrec(args.input)

    except Exception as e:
        #warnings module needed to not impede program
        warnings.warn(f"Errors: {e}")



    warnings.warn(f"understood {len(medicine)} rows and ignored {len(dupes)} rows")

    for item in dupes[:5]:
        warnings.warm(f"item {item.line}skipped for {item.reason}")

    if not medicine:
        warnings.warn("no usable rows found")
        return None


#writing rows and results of dry run

def writing(medicine,args) -> int:
    outpath = args.outfile or "stdout"

    if args.dryrun:
        warnings.warn(f"The output file would have {len(medicine)} records to {outpath}" )
        return 0

    try:
        with outfile(args.outfile) as stream:
            written = clean.writer(medicine,stream)

    except Exception as e:
        warnings.warn(f"Errors: {e}")
        return 1
    warnings.warn(f"{written} rows written to {outpath}")
    return 0



#passing arguments into functions and then returning a value 

def fdedup(args) -> int:
    medicine = report(args)
    if medicine is None:
            return 1
    dedupret, reported = clean.dedup(medicine, key= args.key)
    
    return writing(dedupret, args)

def fretcol(args):
    for i, row in enumerate(clean.COLUMNS, start=1):
        print(f"{i}. {row}")
    
    return 0

def fclean(args) -> int:

    medicine = report(args)
    if medicine is None:
        return 1
    deduped, reported = clean.dedup(medicine, key = args.key)
    return writing(clean.sorter(deduped, by= args.by),args)
   

def fsorted(args) -> int:
    medicine = report(args)
    if medicine is None:
        return 1
    return writing(clean.sorter(medicine, by= args.by),args)
    








def main(argv: list [str] | None = None) -> int:

    parser = argparse.ArgumentParser(prog= "pharmacli",description= "CLI Tool for Retail Pharmacy Data",epilog = "-n, --dryrun to return what you would see if it ran")
    parser.add_argument("-v","--version", action="version",version=f"pharmacli product version: {VERSION}")
    #prints version as CLI tools do
    fileparse = argparse.ArgumentParser(add_help=False)
    #error would occur if defaulted


    #file io and written structure
    fileparse.add_argument("input", help="write path to intro csv")
    fileparse.add_argument("-o","--outfile", help = "written to output file") #std outs to nothing 
    fileparse.add_argument("-n","--dryrun", help = "report what exactly would change",action = "store_true")
    #defaults to sorted by name

    commparse = parser.add_subparsers(dest = "command", required = True, metavar="COMMAND")
    #subparser to add commands

    dedup_k = sorted(clean.deduped)
    sort_k = sorted(clean.SORTCOLUMN)

    dedup = commparse.add_parser("dedup",parents=[fileparse], help= "removes all duplicates")
    dedup.add_argument("--key", default= "row", choices= dedup_k, help = "what makes the rows duplicates")
    dedup.set_defaults(func=fdedup)
    sortd = commparse.add_parser("sort", parents=[fileparse], help= "sorts all data by column name")
    sortd.add_argument("--by", default="description",choices = sort_k,help = "select the column in which you want to sort the column by, default is by description")
    sortd.set_defaults(func=fsorted)
    #add default as name

    retcol = commparse.add_parser("col", help= "return columns") 
    #no parents needed instead infile needs to be added
    retcol.add_argument("input", help="path to intro csv")
    retcol.set_defaults(func=fretcol)

    pclean = commparse.add_parser("clean", parents=[fileparse],help= "cleans data by dedup then sort, mocked up by dryrun value")
    pclean.add_argument("--by", default="row",choices=dedup_k)
    pclean.add_argument("--by", default="description",choices=sort_k, help= "defaulted to by description")
    pclean.set_defaults(func=fclean)


    
    args = parser.parse_args(argv)

    try:
        return args.func(args)
        #each function has its own exit code and that allows it to run safely
    except Exception as e:
        warnings.warn(f"Errors: {e}")
        return 1

    
if __name__ == "__main__":
    raise SystemExit(main())






