"""Verify the four previously expanded SQLite members against the original 7z listing."""
import argparse
import json
from pathlib import Path
import subprocess
import zlib

ROOT = Path(__file__).resolve().parents[1]
REUSED = tuple(f'bus_movements_2026_10_{day}.db' for day in (22,23,26,29))


def member_inventory(seven, archive):
    raw = subprocess.check_output([str(seven),'l','-slt',str(archive)],text=True,encoding='utf-8')
    result = {}
    for group in raw.split('\n\n'):
        values = dict(line.split(' = ',1) for line in group.splitlines() if ' = ' in line)
        name = values.get('Path')
        if name in REUSED:
            if name in result:
                raise ValueError('Duplicate source archive member '+name)
            result[name] = (int(values['Size']),values['CRC'].upper())
    if set(result)!=set(REUSED):
        raise ValueError('Archive listing lacks a reused daily member.')
    return result


def verify(seven,archive,existing):
    source = member_inventory(seven,archive)
    result = []
    for name in REUSED:
        length,wanted = source[name]
        crc = 0
        total = 0
        with (existing/name).open('rb') as f:
            for chunk in iter(lambda:f.read(4*1024**2),b''):
                total += len(chunk)
                crc = zlib.crc32(chunk,crc)
        actual = f'{crc:08X}'
        if (total,actual)!=(length,wanted):
            raise ValueError(f'{name}: archive expected {length} bytes and {wanted}, found {total} and {actual}')
        result.append({'member':name,'bytes':total,'archive_crc32':wanted,'file_crc32':actual,'matched':True})
        print(name,total,actual,'matched',flush=True)
    return {'archive':archive.name,'verification':'CRC32 and byte length from original 7z member listing',
            'reused_day_files':result,'bytes_verified':sum(x['bytes'] for x in result)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seven',type=Path,default=Path('C:/Program Files/7-Zip/7z.exe'))
    parser.add_argument('--archive',type=Path,default=ROOT/'delhi-dtc-bus-gtfs-rt-2026-10.7z')
    parser.add_argument('--existing',type=Path,default=ROOT/'.near-miss-peak-20261121')
    parser.add_argument('--json',type=Path)
    args=parser.parse_args()
    evidence=verify(args.seven,args.archive,args.existing)
    if args.json:
        tmp=args.json.with_suffix('.pending')
        tmp.write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
        tmp.replace(args.json)
