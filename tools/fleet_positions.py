"""Restore the individual archive polls represented by per-vehicle database rows."""

from itertools import groupby


def received_positions(observations):
    for receipt,group in groupby(observations,key=lambda row:row[0]):
        # Repeated identical database rows carry one observation. Distinct
        # positions in a single poll are presented together to the live adapter.
        yield receipt,tuple(dict.fromkeys((observed,lat,lon) for _,observed,lat,lon in group))
