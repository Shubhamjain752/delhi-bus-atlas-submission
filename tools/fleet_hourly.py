"""Create private temporary hour shards for the all-route GPS replay."""
import argparse
from datetime import date, datetime, time
import gzip
import json
from pathlib import Path
import sys
import tempfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.fleet_gps import reader, MISSING_ROUTE
from tools.month_boarding_scan import ARCHIVE, MONTH, PREVIOUS_EXTRACTS, archive_members, daily_database, save
from tools.train_eta import boundary, open_db
from transit.eta import IST


def write_day(day, archive, members, seven, directory):
    marker = directory/f'{day}.json'
    if marker.is_file():
        return
    midnight = int(datetime.combine(date.fromisoformat(day),time(),IST).timestamp())
    # The previous census is an independent completeness check; it is not a route whitelist.
    with gzip.open(ROOT/'data/month-boarding'/f'{day}.json.gz','rt') as f:
        audit = json.load(f)['census']
    hours, totals = [], np.zeros(65536,'i8')
    scan = reader()
    print(day,'opening archive',flush=True)
    with daily_database(seven,archive,members[day],PREVIOUS_EXTRACTS,Path(tempfile.gettempdir())) as (p,source):
        with open_db(p) as db:
            cuts = [boundary(db,f'{h:02d}:00:00') for h in range(24)] + [db.execute('select max(rowid)+1 from vehicle_feed').fetchone()[0]]
        for hour,(lo,hi) in enumerate(zip(cuts,cuts[1:])):
            expected = audit['by_hour'].get(f'{hour:02d}',0)
            raw,valid,records = scan(p,lo,hi,midnight,hour,expected)
            if int(raw.sum()) != expected:
                raise ValueError(f'Hourly SQLite count differs from existing census on {day}/{hour}')
            target = directory/f'{day}-{hour:02d}.npy'
            with target.with_suffix('.pending').open('wb') as out:
                np.save(out,records,allow_pickle=False)
            target.with_suffix('.pending').replace(target)
            totals += raw
            hours.append({str(r) if r != MISSING_ROUTE else 'missing':[int(raw[r]),int(valid[r])]
                          for r in np.flatnonzero(raw)})
            if hour in (0,7,15,23):
                print(day,hour,'receipt hours scanned',flush=True)
    actual = {str(r) if r != MISSING_ROUTE else 'missing':int(totals[r]) for r in np.flatnonzero(totals)}
    if actual != audit['by_route']:
        raise ValueError(f'Route totals differ from previous independent census on {day}')
    save(marker,{'day':day,'crc32':members[day]['crc32'],'hours':hours})


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch',type=Path,required=True)
    parser.add_argument('--days',nargs='+',choices=MONTH,default=MONTH)
    parser.add_argument('--seven',type=Path,default=Path('C:/Program Files/7-Zip/7z.exe'))
    parser.add_argument('--archive',type=Path,default=ARCHIVE)
    a = parser.parse_args()
    a.scratch.mkdir(parents=True,exist_ok=True)
    members = archive_members(a.seven,a.archive)
    for d in a.days:
        write_day(d,a.archive,members,a.seven,a.scratch)
