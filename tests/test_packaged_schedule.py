import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from transit.schedule import cache_schedule, portable_cache_name
from test_router import feed


class PackagedSchedule(unittest.TestCase):
    def test_packaged_index_matches_bytes_after_file_copy(self):
        with TemporaryDirectory() as root:
            root = Path(root)
            source = root / "schedule.zip"
            feed(source)
            index = cache_schedule(source, root / "data")
            copy = root / "copied.zip"
            copy.write_bytes(source.read_bytes())
            os.utime(copy, (1700000000, 1700000000))
            with patch("transit.schedule.build", side_effect=AssertionError("unnecessary index rebuild")):
                self.assertEqual(cache_schedule(copy, root / "data"), index)
            copy.write_bytes(copy.read_bytes() + b"x")
            self.assertNotEqual(portable_cache_name(copy), index.name)
