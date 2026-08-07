# pharmacli

## how to run
    * git clone
    * start python virtual env in your root directory
    * parse medical data by inputing in the commands

### command structure
    ./src/cli.py COMMAND1 pathtofile.csv --command2 value -o pathtooutput.csv

    COMMAND1*(command2)
        - dedup (row, ndc, and ndc date)
        - sort (description, price)
        - col
        - clean (description, price)
    -n for dry run


