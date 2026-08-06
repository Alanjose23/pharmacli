import argparse
import csv

def main():
    parser = argparse.ArgumentParser(description= "Natively processing data")
    parser.add_argument('file', nargs= "?",default= "data/datanadac2026.csv",help= "Parsing dataset into brain",)
    #adding parser to process files
    args = parser.parse_args()

    #converted encoding into utf8
    with open(args.file, mode="r",encoding= "utf-8",newline="") as r:
        reader = csv.DictReader(r)
        for row in reader:
            print(row)
    #with is more reliable than try catch to properly display files
    print("Hello World")




if __name__ == "__main__":
    main()



    