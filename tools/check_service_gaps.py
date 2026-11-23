"""Replay locally received GPS snapshots and count distinct platform intervals visible at receipt."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from transit.api import Planner
from transit.arrivals import platform_estimate
from transit.eta import IST
from transit.replay import CapturedOverlay, received_files, restore_entries


def audit(planner, folder):
    packets = [entry for entry in received_files(folder) if entry[1] == 'positions']
    if not packets:
        raise ValueError(f'No receipted position packets in {folder}')
    overlay = CapturedOverlay(planner.model, planner.directory, 0).bind(planner.router)
    boardable = {(p.route, stop) for p in planner.router.patterns for i, stop in enumerate(p.stops[:-1])
                 if not p.pickups[i]}
    pairs = set()
    platforms_seen = set()
    first = None
    for packet in packets:
        overlay.issue = packet[0]
        when = datetime.fromtimestamp(overlay.issue, IST)
        restore_entries(overlay, [packet], overlay.issue)
        for key in boardable.intersection(overlay.service_gap_snapshot[-1]):
            platforms_seen.add(key)
            route, stop = key
            gap = overlay.observed_service_gap(route, stop, when)
            if gap:
                pairs.add((route, stop, gap['previous_observed_at'], gap['latest_observed_at']))
                if first is None or first['issued_at'] == when.isoformat() and key < (first['route_id'], first['stop_id']):
                    board = platform_estimate(planner.router, overlay, stop, route, when)
                    first = {'route_id': route, 'route': board['route'], 'stop_id': stop,
                             'platform': board['platform']['name'], 'issued_at': when.isoformat(),
                             'observed_service_gap': board['observed_service_gap']}
    return {'capture':folder.name, 'first_receipt':datetime.fromtimestamp(packets[0][0], IST).isoformat(),
            'last_receipt':when.isoformat(), 'position_packets':len(packets),
            'boardable_route_platforms_with_passage':len(platforms_seen),
            'boardable_route_platforms_with_interval':len({(r, s) for r, s, _, _ in pairs}),
            'route_ids_with_interval':len({r for r, _, _, _ in pairs}),
            'distinct_intervals_displayable':len(pairs), 'first_example':first}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('captures', type=Path, nargs='+', help='Directories with positions-RECEIPT-HEADER.pb.gz files')
    parser.add_argument('--out', type=Path, help='Write the aggregate report as JSON')
    args = parser.parse_args()
    planner = Planner(ROOT, online=False)
    try:
        results = [audit(planner, folder) for folder in args.captures]
    finally:
        planner.close()
    report = json.dumps({'method':'Position packets replayed in local receipt order. Count each distinct pair of GPS fix times visible on a boardable route and platform once, regardless of how many polls display it. This measures observed coverage, not prediction accuracy.',
                         'captures':results}, indent=2, ensure_ascii=False) + '\n'
    if args.out:
        args.out.write_text(report, encoding='utf-8')
    print(report, end='')
