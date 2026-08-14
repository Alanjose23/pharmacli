# PharmaCLI Tool v2

This is a pharmacy CLI tool that allows for sorting, deduplication, and data cleaning of a CSV resource file. The operations below allow the insertion of a file that is then parsed using argparse. The tools also allow you to organize and sort by different columns. Currently there is an error with sort by date as the date formats are incorrectly functioning.

## how to run
    * git clone
    * start python virtual env in your root directory (pip install -e .)
    * parse medical data by inputing in the commands as formatted

### command structure 
    - file is made into executable using chmod+x, if not use python
    pharmacli COMMAND1 pathtofile.csv (--key (row OR ndc)) (--by (description OR date OR price OR ndc)) -o pathtooutput.csv (-n or --dryrun)

    COMMAND1
        - dedup (row, ndc, and ndc+date) --key
        - sort (description, price) --by
        - columns
        - clean (description, price) both --by and --key tags
    -n for dry run

### example 
    - dedup method 
    (key row)
        - pharmacli dedup example.csv -o result.csv 

    - sort method
     (by description)
        - pharmacli sort example.csv -o result.csv

    - col method
        - pharmacli col

    - clean method 
    (needs both key and description if you want to sort by both methods)
        - pharmacli clean example.csv -o result.csv

    - dry run example
        - append -n or --dryrun to test command
            - pharmacli command example.csv -o result.csv -n(--dryrun)
