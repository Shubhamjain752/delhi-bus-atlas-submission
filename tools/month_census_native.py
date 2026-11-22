"""Optional compiled row counter for large SQLite archives. Uses the public SQLite API."""
import ctypes as ct
from ctypes.util import find_library
from functools import lru_cache
import os
from pathlib import Path
import sys


@lru_cache(maxsize=1)
def scanner():
    import numpy as np
    from numba import njit

    library = str(Path(sys.base_prefix) / "DLLs" / "sqlite3.dll") if os.name == "nt" else find_library("sqlite3")
    if not library:
        raise OSError("SQLite shared library unavailable")
    sqlite = ct.CDLL(library)
    address = ct.c_size_t
    byte_string = ct.c_char_p

    def bind(name, result, *arguments):
        call = getattr(sqlite, "sqlite3_" + name)
        call.argtypes = list(arguments)
        call.restype = result
        return call

    open_db = bind("open_v2", ct.c_int, byte_string, ct.POINTER(address), ct.c_int, byte_string)
    prepare = bind("prepare_v2", ct.c_int, address, byte_string, ct.c_int, ct.POINTER(address), ct.c_void_p)
    finalize = bind("finalize", ct.c_int, address)
    close = bind("close", ct.c_int, address)
    step = bind("step", ct.c_int, address)
    column_type = bind("column_type", ct.c_int, address, ct.c_int)
    number = bind("column_double", ct.c_double, address, ct.c_int)
    as_bytes = bind("column_text", ct.POINTER(ct.c_uint8), address, ct.c_int)
    length = bind("column_bytes", ct.c_int, address, ct.c_int)

    @njit(nogil=True)
    def count(statement, routes, hours):
        first = 86400
        last = -1
        missing = 0
        status = step(statement)
        while status == 100:  # SQLITE_ROW
            kind = column_type(statement, 0)
            if kind == 5:
                missing += 1
            elif kind == 1 or kind == 2:
                route = number(statement, 0)
                if not (0 <= route < len(routes)) or int(route) != route:
                    return -1, first, last, missing
                routes[int(route)] += 1
            else:
                return -1, first, last, missing
            if length(statement, 1) != 8:
                return -1, first, last, missing
            t = as_bytes(statement, 1)
            if t[2] != 58 or t[5] != 58:
                return -1, first, last, missing
            for index in (0, 1, 3, 4, 6, 7):
                if t[index] < 48 or t[index] > 57:
                    return -1, first, last, missing
            hour = (int(t[0]) - 48) * 10 + int(t[1]) - 48
            minute = (int(t[3]) - 48) * 10 + int(t[4]) - 48
            second = (int(t[6]) - 48) * 10 + int(t[7]) - 48
            if hour >= 24 or minute >= 60 or second >= 60:
                return -1, first, last, missing
            stamp = hour * 3600 + minute * 60 + second
            hours[hour] += 1
            first = min(first, stamp)
            last = max(last, stamp)
            status = step(statement)
        return status, first, last, missing

    def scan(path):
        handle, statement = address(), address()
        uri = (path.resolve().as_uri() + "?mode=ro&immutable=1").encode()
        try:
            if open_db(uri, ct.byref(handle), 0x41, None):  # READONLY | URI
                raise OSError("Could not open daily archive for census")
            if prepare(handle, b"SELECT route_id,time FROM vehicle_feed", -1, ct.byref(statement), None):
                raise ValueError("Missing route_id or time in daily archive")
            routes = np.zeros(65536, dtype=np.int64)
            hours = np.zeros(24, dtype=np.int64)
            status, first, last, missing = count(statement.value, routes, hours)
            if status == -1:
                return None  # Nonstandard values use SQLite's general GROUP BY implementation.
            if status != 101:  # SQLITE_DONE
                raise OSError(f"SQLite returned status {status} during the route census")
        finally:
            if statement.value:
                finalize(statement)
            if handle.value:
                close(handle)
        by_route = {str(i): int(routes[i]) for i in np.flatnonzero(routes)}
        if missing:
            by_route["missing"] = missing
        by_hour = {f"{i:02d}": int(hours[i]) for i in np.flatnonzero(hours)}
        def clock(t):
            return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}" if last >= 0 else None
        return {"raw_records": int(hours.sum()), "by_hour": by_hour, "by_route": by_route,
                "first_receipt_clock": clock(first), "last_receipt_clock": clock(last)}
    return scan
