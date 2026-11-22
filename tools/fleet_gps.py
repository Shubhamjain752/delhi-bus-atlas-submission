"""Read the official archive through SQLite's public, read-only C interface.

SQLite drives the cursor. Numba avoids creating a Python tuple and seven Python
objects for each of the 913 million broadcasts. All aggregates use receipt hour.
"""
import ctypes as ct
from ctypes.util import find_library
from functools import lru_cache
import os
from pathlib import Path
import sys

import numpy as np
from numba import njit


# Two independent 64-bit identity hashes never leave the in-memory GPS shards as IDs.
FIX = np.dtype([('bus','u8'),('trip','u8'),('route','i4'),('minute','i4'),
                ('obs','i4'),('recv','i4'),('lat','f8'),('lon','f8')])
MISSING_ROUTE = 65535


@lru_cache(maxsize=1)
def reader():
    library = str(Path(sys.base_prefix)/'DLLs'/'sqlite3.dll') if os.name == 'nt' else find_library('sqlite3')
    db = ct.CDLL(library)
    ptr = ct.c_size_t
    def bind(name, result, *args):
        fn = getattr(db,'sqlite3_'+name)
        fn.argtypes, fn.restype = list(args), result
        return fn
    open_db = bind('open_v2',ct.c_int,ct.c_char_p,ct.POINTER(ptr),ct.c_int,ct.c_char_p)
    close = bind('close',ct.c_int,ptr)
    prepare = bind('prepare_v2',ct.c_int,ptr,ct.c_char_p,ct.c_int,ct.POINTER(ptr),ct.c_void_p)
    step = bind('step',ct.c_int,ptr)
    finalize = bind('finalize',ct.c_int,ptr)
    integer = bind('column_int64',ct.c_int64,ptr,ct.c_int)
    number = bind('column_double',ct.c_double,ptr,ct.c_int)
    kind = bind('column_type',ct.c_int,ptr,ct.c_int)
    chars = bind('column_text',ct.POINTER(ct.c_uint8),ptr,ct.c_int)
    size = bind('column_bytes',ct.c_int,ptr,ct.c_int)

    @njit(inline='always')
    def identity(s, length):
        key = np.uint64(14695981039346656037)
        for i in range(length):
            key = (key ^ np.uint64(s[i])) * np.uint64(1099511628211)
        return key

    @njit(inline='always')
    def trip_minute(s, length, route):
        # route_HH_MM[_provider-suffix]; verify the ID's route prefix.
        k = 0
        value = 0
        while k < length and 48 <= s[k] <= 57:
            value = value*10 + int(s[k])-48
            k += 1
        if value != route or k == 0 or k >= length or s[k] != 95:
            return -1
        k += 1
        start = k
        hh = 0
        while k < length and 48 <= s[k] <= 57:
            hh = hh*10 + int(s[k])-48
            k += 1
        if not 1 <= k-start <= 2 or k+2 >= length or s[k] != 95:
            return -1
        if not (48 <= s[k+1] <= 57 and 48 <= s[k+2] <= 57):
            return -1
        mm = (int(s[k+1])-48)*10 + int(s[k+2])-48
        if mm >= 60 or (k+3 < length and s[k+3] != 95):
            return -1
        return hh*60+mm

    @njit(nogil=True)
    def consume(st, midnight, hour, raw, valid, out):
        n = 0
        status = step(st)
        previous = -1
        while status == 100:
            route = MISSING_ROUTE
            if kind(st,0) != 5:
                r = number(st,0)
                if not 0 <= r < MISSING_ROUTE or int(r) != r:
                    return -6,n
                route = int(r)
            raw[route] += 1
            if size(st,4) != 8:
                return -2,n
            t = chars(st,4)
            local = ((int(t[0])-48)*10+int(t[1])-48)*3600 + ((int(t[3])-48)*10+int(t[4])-48)*60+(int(t[6])-48)*10+int(t[7])-48
            if local//3600 != hour or local < previous:
                return -3,n
            previous = local
            recv = midnight+local
            obs = integer(st,3)
            if 0 <= recv-obs <= 90:
                vlen = size(st,1)
                tlen = size(st,2)
                if vlen and tlen:
                    lat = number(st,5)
                    lon = number(st,6)
                    if 28 <= lat <= 30 and 76 <= lon <= 78:
                        if n >= len(out):
                            return -4,n
                        v = chars(st,1)
                        trip = chars(st,2)
                        out[n].bus = identity(v,vlen)
                        out[n].trip = identity(trip,tlen)
                        out[n].route = route
                        out[n].minute = trip_minute(trip,tlen,route)
                        out[n].obs = obs
                        out[n].recv = recv
                        out[n].lat = lat
                        out[n].lon = lon
                        valid[route] += 1
                        n += 1
            status = step(st)
        return status,n

    def read(path, first, end, midnight, hour, capacity):
        handle, st = ptr(), ptr()
        uri = (Path(path).resolve().as_uri()+'?mode=ro&immutable=1').encode()
        raw, valid = np.zeros(65536,'i8'), np.zeros(65536,'i8')
        records = np.empty(capacity,dtype=FIX)
        try:
            if open_db(uri,ct.byref(handle),0x41,None):
                raise OSError('Cannot open daily SQLite input')
            query = ('SELECT route_id,vehicle_id,trip_id,timestamp,time,lat,lng FROM vehicle_feed '
                     f'WHERE rowid >= {int(first)} AND rowid < {int(end)}').encode()
            if prepare(handle,query,-1,ct.byref(st),None):
                raise ValueError('Unexpected daily input schema')
            status,n = consume(st.value,midnight,hour,raw,valid,records)
            if status != 101:
                raise ValueError(f'Fleet scan could not validate hour {hour}; status {status}')
        finally:
            if st.value: finalize(st)
            if handle.value: close(handle)
        return raw, valid, records[:n]
    return read
