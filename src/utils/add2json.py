#!/usr/bin/env python
from __future__ import print_function
import json
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-a", "--aug", action="store", required=True,
                        help="Augmenting data")
    parser.add_argument("-j", "--json", action="store", required=True,
                        help="Other json with which to merge (overwrite)")

    args = parser.parse_args()

    with open(args.json, "r") as f:
        data_json = json.load(f)
    
    ofilename = args.aug + ".tgt"
    ifilename = args.aug + ".src"
    data_json['aug'] = {}        
    data_json['aug']['ifilename'] = ifilename
    data_json['aug']['ofilename'] = ofilename
    data_json['aug']['sentences'] = {}

    with open(ofilename, "r") as fo:
        with open(ifilename, "r") as fi:
            ooffset = fo.tell()
            ioffset = fi.tell()
            line_num = 0
            iline = fi.readline()
            oline = fo.readline()
            while(iline and oline):
                print("\rLine: ", line_num, end="")
                olen = len(oline.strip().split())
                ilen = len(iline.strip().split())
                data_json['aug']['sentences'][str(line_num)] = {
                    'ilen': ilen,
                    'olen': olen,
                    'ioffset': ioffset,
                    'ooffset': ooffset          
                }
                ooffset = fo.tell()
                ioffset = fi.tell()
                iline = fi.readline()
                oline = fo.readline()
                line_num += 1
    print()

    with open(args.json, "w") as f:
        json.dump(data_json, f, indent=4)


if __name__ == "__main__":
    main()
