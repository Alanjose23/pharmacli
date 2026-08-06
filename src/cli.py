import argparse

VERSION = "1.0"



#add an object to close the writer


#add an error handler object to fix certain functions




#passing arguments into functions and then returning a value 

def fdedup(args):
    print(args)
    return 0

def fretcol(args):
    print(args)
    return 0

def fclean(args):
    print(args)
    return 0

def fsorted(args):
    print(args)
    return 0








def main(argv:None):

    parser = argparse.ArgumentParser(prog= "pharmacli",description= "CLI Tool for Retail Pharmacy Data",epilog = "-n, --dryrun to return what you would see if it ran")
    parser.add_argument("-v","--version", action="version",version=f"pharmacli product version: {version}")
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

    dedup = commparse.add_parser("dedup",parents=[fileparse], help= "removes all duplicates")
    dedup.set_defaults(func=fdedup)
    sortd = commparse.add_parser("sort", parents=[fileparse], help= "sorts all data by column name")
    sortd.add_argument("--by", default="NDC Description",help = "select the column in which you want to sort the column by, default is by description")
    sortd.set_defaults(func=fsorted)
    #add default as name

    retcol = commparse.add_parser("col", help= "return columns") 
    #no parents needed instead infile needs to be added
    retcol.add_argument("input", help="path to intro csv")
    retcol.set_defaults(func=fretcol)

    clean = commparse.add_parser("clean", parents=[fileparse],help= "cleans data by dedup then sort, mocked up by dryrun value")
    clean.add_argument("--by", default="NDC Description", help= "defaulted to by description")
    clean.set_defaults(func=fclean)


    args = parser.parse_args(argv)
    return args.func(args)
        #each function has its own exit code and that allows it to run safely

    
if __name__ == "__main__":
    raise SystemExit(main())






