import argparse

version = "1.0"

parser = argparse.ArgumentParser(description= "CLI Tool for Retail Pharmacy Data",epilog = "use dr to clean, sort, and dedup all at the same time to ")
parser.add_argument("-v","--version", action="version",version=f"{version}")
fileparse = argparse.ArgumentParser()
fileparse.add_argument("infile", help="write path to intro csv")
fileparse.add_argument("outfile", help = "written to output file")
fileparse.add_argument("-n","--dryrun", help = "report what exactly would change")

commparse = parser.add_subparsers(dest = "command", required = True)
#subparser to add commands

dedup = commparse.add_parser("dedup",parents=[fileparse], help= "removes all duplicates")
sortd = commparse.add_parser("sort", parents=[fileparse], help= "sorts all data by column name")
retcol = commparse.add_parser("col", parents=[fileparse],help= "return columns, auto defaults to item by name")
clean = commparse.add_parser("col", parents=[fileparse],help= "cleans data by dedup then sort, mock by dryrun value")







#basic command line arguments with help structure to add aid
# parser.add_argument("dedup", help = "remove duplicates from a file and add to a new file")
# parser.add_argument("sort", help = "sort items in a file and add to a new file")
# parser.add_argument("run", help = "run dedup and sort functionality on a file to a new file")
# parser.add_argument("-dr","--dryrun", help = "Dry Run functionality of CLI tool")
# parser.add_argument("-v", "--version", help= "Lists the version of the tool")

arguments = parser.parse_args()

print(arguments)