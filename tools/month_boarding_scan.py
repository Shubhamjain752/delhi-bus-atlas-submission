"""Run the fixed October boarding experiment one archive member at a time."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from functools import lru_cache
import gzip
import hashlib
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import month_census
from tools.near_miss_peak import PeakStudy, token
from tools.train_eta import gtfs_routes

MONTH = tuple(f'2026-10-{n:02}' for n in range(1,32))
WINDOWS = {'AM':(28800,34200), 'noon':(43200,48600), 'PM':(61200,66600), 'night':(79200,84600)}
ARCHIVE = ROOT/'delhi-dtc-bus-gtfs-rt-2026-10.7z'
OUTPUT = ROOT/'data'/'month-boarding'
PREVIOUS_EXTRACTS = ROOT/'.near-miss-peak-20261121'
PROTOCOL = 'october-boarding-4-windows-52-routes-v1'


def archive_members(seven, archive):
    text = subprocess.check_output([str(seven),'l','-slt',str(archive)],text=True,encoding='utf-8')
    if 'Solid = -' not in text:
        raise ValueError('Expected individually seekable archive members.')
    members = {}
    for section in text.replace('\r\n','\n').split('\n\n'):
        record = dict(line.split(' = ',1) for line in section.splitlines() if ' = ' in line)
        name = record.get('Path','')
        if name.startswith('bus_movements_') and name.endswith('.db'):
            day = name[14:-3].replace('_','-')
            if day in members:
                raise ValueError('Duplicate daily member: '+day)
            members[day] = {'member':name,'bytes':int(record['Size']),'packed_bytes':int(record['Packed Size']),
                            'crc32':record['CRC'],'block':int(record['Block'])}
    if set(members)!=set(MONTH) or len({m['block'] for m in members.values()})!=31:
        raise ValueError('Expected all 31 October days in separate compression blocks.')
    return members


@contextmanager
def daily_database(seven, archive, member, earlier, temporary_parent):
    existing = earlier/member['member']
    if existing.is_file() and existing.stat().st_size == member['bytes']:
        yield existing,'existing_read_only'
        return
    if shutil.disk_usage(temporary_parent).free < member['bytes'] + 1024**3:
        raise OSError('Temporary volume needs one expanded database plus 1 GiB of working space.')
    # Only this freshly allocated random file is disposed. User-owned extracts are never removed.
    with tempfile.NamedTemporaryFile(prefix='delhi-month-',suffix='.db',dir=temporary_parent,
                                     delete=True,delete_on_close=False) as dst:
        subprocess.run([str(seven),'x','-so',str(archive),member['member']],stdout=dst,check=True)
        dst.flush()
        path = Path(dst.name)
        if path.stat().st_size != member['bytes']:
            raise ValueError('Incomplete expanded day: '+member['member'])
        yield path,'temporary_single_member'


def route_groups(router, primary):
    diagnostic = sorted(route for route,(name,_) in router.routes.items() if name in ('502UP','502DOWN'))
    if len(primary)!=40 or len(diagnostic)!=2 or set(diagnostic)&set(primary):
        raise ValueError('Unexpected 40-route primary cohort or 502 route assignment')
    # Static selection only. Global volume, fresh GPS and observed outcomes play no part.
    official = {p.route for p in router.patterns if len(p.stops)>=3}
    options = sorted(official-set(primary)-set(diagnostic),key=lambda r:(token('october-month-extension-v1',r),r))
    return {'original_40':list(primary),'additional_static_10':options[:10],'502_diagnostic':diagnostic}


class MonthStudy(PeakStudy):
    windows = WINDOWS

    def __init__(self, root, model, router):
        self.data = root/'data'
        self.gtfs = root/'delhi_buses_static_gtfs_v1.zip'
        self.model, self.router = model,router
        original = json.loads((self.data/'near-miss-peak.json').read_text(encoding='utf-8'))['routes']
        self.groups = route_groups(router,original)
        self.route_group = {route:group for group,routes in self.groups.items() for route in routes}
        self.routes = list(self.route_group)
        self.patterns = gtfs_routes(self.gtfs,self.routes)

    @lru_cache(maxsize=100000)
    def assigned_peak(self, day, vehicle):
        return tuple(self.windows)[int(token(day,vehicle,'month-boarding-window-v1'),16)%len(self.windows)]


def profile_info(model, day):
    weekend = day.weekday()>=5
    chosen = model.weekend if weekend else model
    return {'day_type':'weekend' if weekend else 'weekday','train':chosen.metadata['train'],
            'learned_profile_available': day > chosen.last_train_day,
            'mode':'historical_if_supported' if day > chosen.last_train_day else 'timetable_only'}


def save(path, data, compressed=False):
    tmp = path.with_name(path.name+'.pending')
    with tmp.open('wb') as stream:
        raw = json.dumps(data,separators=(',',':'),allow_nan=False).encode()
        if compressed:
            with gzip.GzipFile(filename='',mode='wb',compresslevel=5,fileobj=stream,mtime=0) as out:
                out.write(raw)
        else:
            stream.write(json.dumps(data,indent=2,allow_nan=False).encode())
    tmp.replace(path)


def inputs(study, members):
    names = ['tools/month_boarding_scan.py','tools/near_miss_peak.py',
             'tools/replay_peak_router.py','tools/train_eta.py','transit/eta.py','transit/router.py',
             'transit/live.py','transit/replay.py','transit/arrivals.py','transit/walking.py','transit/schedule.py',
             'data/eta_model.json.gz','data/weekend/eta_model.json.gz','data/walk.npz','delhi_buses_static_gtfs_v1.zip']
    hashes = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    return {'protocol':PROTOCOL,'archive':ARCHIVE.name,'archive_digest_from_supplied_checksums':
                (ROOT/'SHA256SUMS.txt').read_text().split()[0],
            'archive_members':members,'source_hashes':hashes,'windows_seconds_ist':WINDOWS,
            'route_groups':study.groups,'routes':{k:study.router.routes[k] for k in study.routes}}


def run(args):
    from tools.replay_peak_router import MonthRouter, load_prospective_profiles
    started = time.monotonic()
    members = archive_members(args.seven,args.archive)
    model = load_prospective_profiles(ROOT/'data')
    engine = MonthRouter(root=ROOT,model=model)
    study = MonthStudy(ROOT,model,engine.router)
    manifest = inputs(study,members)
    signature = hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    args.output.mkdir(parents=True,exist_ok=True)
    manifest_path = args.output/'manifest.json'
    if manifest_path.is_file() and json.loads(manifest_path.read_text())['signature'] != signature:
        raise ValueError('Existing month manifest belongs to a different protocol or code revision.')
    if not manifest_path.exists():
        save(manifest_path,{'signature':signature,**manifest})
    for day in args.days:
        target = args.output/(day+'.json.gz')
        if target.is_file():
            with gzip.open(target,'rt',encoding='utf-8') as src:
                existing = json.load(src)
            if existing['signature'] != signature or existing['day'] != day or not existing['complete']:
                raise ValueError('A saved daily result failed its protocol checks.')
            print(day,'already complete',flush=True)
            continue
        begin = time.monotonic()
        print(day,'opening',members[day]['member'],flush=True)
        with daily_database(args.seven,args.archive,members[day],args.existing,args.temp_dir) as (db,source):
            extracted = time.monotonic()
            print(day,source,'SQLite ready',round(extracted-begin,1),'s',flush=True)
            with ThreadPoolExecutor(max_workers=1) as pool:
                census_module = importlib.reload(month_census)
                census_hash = hashlib.sha256(Path(census_module.__file__).read_bytes()).hexdigest()
                census_task = pool.submit(census_module.scan_census,db)
                rows, coverage = [],{}
                for peak in WINDOWS:
                    new_rows,coverage[peak] = study.scan(db,day,peak,include_model_source=True)
                    rows.extend(new_rows)
                    print(day,peak,len(new_rows),'decisions',flush=True)
                rows.sort(key=lambda r:(r['ready'],r['bus']))
                if len({r['bus'] for r in rows}) != len(rows):
                    raise ValueError('A physical bus received two decisions on the same date.')
                for row in rows:
                    row['route_group'] = study.route_group[row['route_id']]
                features_done = time.monotonic()
                paired = engine.evaluate_day(day,rows,db)
                paired_done = time.monotonic()
                census = census_task.result()
            selected = {r['run'] for r in rows if 1<=r['gap']<=30}
            if len(paired['rows']) != len(selected) or {r['run'] for r in paired['rows']} != selected:
                raise ValueError('Paired replay does not cover every 1–30 second decision exactly once.')
            profile = profile_info(model,date.fromisoformat(day))
            if not profile['learned_profile_available'] and any(r['forecast_source']!='timetable' for r in rows):
                raise ValueError('A future model was used before the date of its training.')
            for r in paired['rows']:
                r['route_group'] = study.route_group[r['route_id']]
            result = {'schema':1,'complete':True,'signature':signature,'protocol':PROTOCOL,'day':day,
                      'archive_member':members[day], 'profile':profile,'windows':WINDOWS,'coverage':coverage,
                      'route_groups':study.groups,'opportunities':rows,'router':paired,'census':census,
                      'census_implementation_sha256':census_hash,
                      'elapsed_seconds':{'extract':round(extracted-begin,2),'cohort':round(features_done-extracted,2),
                                         'router':round(paired_done-features_done,2),'total':round(time.monotonic()-begin,2)}}
            save(target,result,compressed=True)
            print(day,'published',len(rows),'decisions;',len(selected),'router cases;',
                  result['elapsed_seconds'],flush=True)
    print('Completed requested days in',round(time.monotonic()-started,1),'seconds.',flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days',nargs='+',choices=MONTH,default=MONTH)
    parser.add_argument('--seven',type=Path,default=Path('C:/Program Files/7-Zip/7z.exe'))
    parser.add_argument('--archive',type=Path,default=ARCHIVE)
    parser.add_argument('--existing',type=Path,default=PREVIOUS_EXTRACTS)
    parser.add_argument('--temp-dir',type=Path,default=Path(tempfile.gettempdir()))
    parser.add_argument('--output',type=Path,default=OUTPUT)
    run(parser.parse_args())
