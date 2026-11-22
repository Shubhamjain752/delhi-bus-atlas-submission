import csv
from datetime import datetime, timedelta
from io import StringIO
import unittest

from tools.month_ticket_check import FIELDS, Offer, bus_token, inspect_stream, summarize
from transit.eta import IST


class MonthlyTicketChecks(unittest.TestCase):
    def test_matching_and_microsecond_boundaries_preserve_independent_denominators(self):
        ready=datetime(2026,10,22,8,tzinfo=IST)
        def offer(bus,stop='board',policies=('baseline','revised'),error=None):
            return Offer('2026-10-22',bus_token(bus),'1up',stop,int(ready.timestamp()),policies,'uncertain',error)
        offers=[offer('BUS'),offer('OTHER',policies=('baseline',)),offer('AMB',error='ambiguous_boarding_name_on_route')]
        stream=StringIO()
        out=csv.writer(stream)
        out.writerow(FIELDS)
        def record(vehicle='BUS',board=' BOARD ',delta=-60,route='1Up'):
            stamp=(ready+timedelta(seconds=delta)).isoformat()
            out.writerow([1,10,vehicle,1,'end',route,board,stamp,'True'])
        record()
        record()
        record(delta=60.000001)
        record(delta=300.000001)
        record(vehicle=' BUS ',delta=0)
        record(vehicle='NULL',delta=0)
        record(vehicle='OTHER',board='B.O.A.R.D',delta=3)
        record(vehicle='AMB',delta=1)
        stream.seek(0)
        health,records=inspect_stream(stream,offers)
        report=summarize(offers,health['dates_in_delhi'],records)
        self.assertEqual(health['csv_records'],8)
        self.assertEqual(health['fields']['vehicle_number']['null_literal'],1)
        self.assertEqual(report['baseline']['mutually_exclusive_offer_results'],{
            'exact_join_within_60s':1,'vehicle_route_found_without_exact_start_name_on_date':1,
            'ambiguous_boarding_name_on_route':1})
        near=report['revised']['windows_seconds']
        self.assertEqual((near['60']['matching_csv_records'],near['60']['distinct_nine_field_contents']),(2,1))
        self.assertEqual((near['60']['offers_with_before_ready_record'],near['60']['offers_with_after_ready_record']),(1,0))
        self.assertEqual((near['300']['matching_csv_records'],near['300']['distinct_nine_field_contents']),(3,2))
        self.assertEqual(near['60']['same_place_time_records_with_missing_vehicle'],1)
        self.assertEqual(near['60']['same_place_time_records_with_different_vehicle'],2)

    def test_date_is_converted_to_delhi_and_naive_time_cannot_join(self):
        instant=datetime(2026,10,22,0,0,1,tzinfo=IST)
        offer=Offer('2026-10-22',bus_token('B'),'up','stop',int(instant.timestamp()),('baseline',),'unverified')
        stream=StringIO()
        writer=csv.writer(stream)
        writer.writerow(FIELDS)
        for value in ('2026-10-21T18:30:01+00:00','2026-10-22T00:00:01','unknown'):
            writer.writerow([1,10,'B',1,'end','UP','STOP',value,'False'])
        stream.seek(0)
        health,records=inspect_stream(stream,[offer])
        r=summarize([offer],health['dates_in_delhi'],records)['baseline']
        self.assertEqual((health['invalid_or_missing_created_at'],health['created_at_without_offset']),(1,1))
        self.assertEqual(r['windows_seconds']['60']['offers_with_exact_ready_record'],1)
        self.assertEqual(r['windows_seconds']['60']['matching_csv_records'],1)
