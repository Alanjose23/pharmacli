# pharmacli
This is a pharmacy CLI tool that allows for sorting, deduplication, and data cleaning of a CSV resource file. The operations below allow the insertion of a file that is then parsed using argparse. The tools also allow you to organize and sort by different columns 

## how to run
    * git clone
    * start python virtual env in your root directory
    * parse medical data by inputing in the commands

### command structure 
    - file is made into executable using chmod+x
    ./src/cli.py COMMAND1 pathtofile.csv (--key (row OR ndc)) (--by description OR date OR price OR ndc) -o pathtooutput.csv

    COMMAND1*(command2)
        - dedup (row, ndc, and ndc date)
        - sort (description, price)
        - col
        - clean (description, price)
    -n for dry run

### example 
    - dedup method 
    (key row)
        - src/cli.py dedup example.csv -o result.csv 

    - sort method
     (by description)
        - src/cli.py sort example.csv -o result.csv

    - col method
        - src/cli.py col

    - clean method 
    (needs both key and description if you want to sort by both methods)
        - sec/cli.py clean example.csv -o result.csv

    - dry run example
        - append -n or --dryrun to test command
            - src/cli.py command example.csv -o result.csv -n(--dryrun)
