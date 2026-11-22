"""Run the local Delhi bus planner. Open http://127.0.0.1:8765."""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--offline", action="store_true", help="Use only local datasets and historical replay.")
    args = parser.parse_args()
    if args.offline:
        os.environ["DELHI_LIVE"] = "0"
    local_packages = Path(__file__).resolve().parent / ".runtime"
    if local_packages.is_dir():
        sys.path.insert(0, str(local_packages))
    import uvicorn
    uvicorn.run("transit.api:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
