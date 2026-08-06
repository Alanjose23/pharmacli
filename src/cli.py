import argparse

parser = argparse.ArgumentParser(description= "CLI Tool for Retail Pharmacy Data",epilog = "use dr to clean, sort, and dedup all at the same time to ")

#basic command line arguments with help structure to add aid
parser.add_argument("dedup", help = "remove duplicates from a file and add to a new file")
parser.add_argument("sort", help = "sort items in a file and add to a new file")
parser.add_argument("run", help = "run dedup and sort functionality on a file to a new file")
parser.add_argument("-dr","--dryrun", help = "Dry Run functionality of CLI tool")
parser.add_argument("-v", "--version", help= "Lists the version of the tool")

arguments = parser.parse_args()

print(arguments)