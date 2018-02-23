from __future__ import print_function
import sys
import json
import os
import argpase


def main():
    parser = argpasrse.ArgumentParser()
    parser.add_argument("-a", "--aug", action="store", required=True,
                        help="Augmenting data")
    parser.add_argument("-j", "--json", action="store", required=True,
                        help="Other json with which to merge (overwrite)")

    args = parser.parse_args()

    with open(args.json, "r") as f:
        data_json = json.load(f)
    
    ofilename = args.aug + ".tgt"
    ifilename = args.aug + ".src"

    with open(ofilename, "r") as fo:
        with open(ifilename, "r") as fi:
            data_json['aug'] = {}        
            ooffset = fo.tell()
            ioffset = fi.tell()
            line_num = 0
            iline = fi.readline()
            oline = fo.readline()
            while(iline and oline):
                print("\rLine: ", line_num, end="")
                olen = len(oline.strip().split())
                ilen = len(iline.strip().split())
                data_json['aug'][str(line_num)] = {
                    'ofilename': ofilename,
                    'ifilename': ifilename,
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

with open(args.output, "w") as f:
    json.dump(data_json, f, indent=4)


if __name__ == "__main__":
    main()

