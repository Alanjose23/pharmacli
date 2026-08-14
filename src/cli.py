import argparse
import os
import sys
from pathlib import Path
from typing import Final

from ..models.rows import COLUMNS

# import reader,writer, registry, pipeline, registry, models, errors
from .err import PharmaError, RuleError
from .output import open_output
from .pipeline import Cleaner
from .protocols import Rule
from .reader import CSVR
from .registry import RULES
from .writer import CSVW

# entry point of code

#exit code definition
    # 0 = success
    # 1 = no usable rows
    # 2 for bad input
    # 3 for bad rule


# display errors

# rule recongition
    # define a rule and its identifying key
    # handle rule misdiagnosis and representation
    # add errors to definition

# build command line parser
    # define output input and dryrun tags
    # add new rules command

# list rules and 

# list columns 


# obtain arguments from command line grammar
    # return rules, columns, or parse report with input line
    # display common error faults( no file found and lines detected

    # add and init rules with request
        # create a configured instance
    # display rule set and if there is a bad rule:reject

    # dryrun, rule handling, output file recongnition, and possible errors
    # return success code


# def main(argv: list [str] | None = None) -> int:

#     parser = argparse.ArgumentParser(prog= "pharmacli",description= "CLI Tool for Retail Pharmacy Data",epilog = "-n, --dryrun to return what you would see if it ran")
#     parser.add_argument("-v","--version", action="version",version=f"pharmacli product version: {VERSION}")
#     #prints version as CLI tools do
#     fileparse = argparse.ArgumentParser(add_help=False)
#     #error would occur if defaulted


#     #file io and written structure
#     fileparse.add_argument("input", help="write path to intro csv")
#     fileparse.add_argument("-o","--outfile", help = "written to output file") #std outs to nothing 
#     fileparse.add_argument("-n","--dryrun", help = "report what exactly would change",action = "store_true")
#     #defaults to sorted by name

#     commparse = parser.add_subparsers(dest = "command", required = True, metavar="COMMAND")
#     #subparser to add commands

#     dedup_k = sorted(clean.deduped)
#     sort_k = sorted(clean.SORTCOLUMN)

#     dedup = commparse.add_parser("dedup",parents=[fileparse], help= "removes all duplicates")
#     dedup.add_argument("--key", default= "row", choices= dedup_k, help = "what makes the rows duplicates")
#     dedup.set_defaults(func=fdedup)
#     sortd = commparse.add_parser("sort", parents=[fileparse], help= "sorts all data by column name")
#     sortd.add_argument("--by", default="description",choices = sort_k,help = "select the column in which you want to sort the column by, default is by description")
#     sortd.set_defaults(func=fsorted)
#     #add default as name

#     retcol = commparse.add_parser("col", help= "return columns") 
#     #no parents needed returning just the columns 
#     retcol.set_defaults(func=fretcol)

#     pclean = commparse.add_parser("clean", parents=[fileparse],help= "cleans data by dedup then sort, mocked up by dryrun value")
#     pclean.add_argument("--key", default="row",choices=dedup_k)
#     pclean.add_argument("--by", default="description",choices=sort_k, help= "defaulted to by description")
#     pclean.set_defaults(func=fclean)


    
#     args = parser.parse_args(argv)

#     try:
#         return args.func(args)
#         #each function has its own exit code and that allows it to run safely
#     except Exception as e:
#         errr(f"error: {e}")
#         return 1