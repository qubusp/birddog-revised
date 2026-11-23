"""Source kit command line.

  python -m birddog.sources new <name> --type http_json   scaffold dags/sources/<name>/
  python -m birddog.sources list                          sources and their status
  python -m birddog.sources validate [<name> ...]         check source.yml files
  python -m birddog.sources run <name> [--steps land,transform] [--full-refresh]
  python -m birddog.sources fetch <name> [--limit 5]      print fetched records, write nothing

run/fetch need BIRDDOG_DATABASE_URL (run) and the fetcher's environment
variables. --dir points at another sources folder (default dags/sources)."""

import argparse
import itertools
import json
import sys
from pathlib import Path

from birddog.sources.spec import FETCH_TYPES, SpecError, default_sources_dir, discover, load_spec


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m birddog.sources", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", type=Path, default=None, help="sources folder (default dags/sources)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new", help="scaffold a new source folder")
    p.add_argument("name")
    p.add_argument("--type", default="http_json", choices=FETCH_TYPES)
    p.add_argument("--force", action="store_true")
    sub.add_parser("list", help="list sources")
    p = sub.add_parser("validate", help="validate source.yml files")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("run", help="land -> transform -> verify -> expose against BIRDDOG_DATABASE_URL")
    p.add_argument("name")
    p.add_argument("--steps", default="land,transform,verify,expose")
    p.add_argument("--full-refresh", action="store_true")
    p = sub.add_parser("fetch", help="run only the fetcher and print records (dry run)")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=5)
    args = ap.parse_args(argv)
    root = args.dir or default_sources_dir()

    if args.cmd == "new":
        from birddog.sources.scaffold import new_source
        folder, files = new_source(args.name, args.type, root, force=args.force)
        print(f"created {folder}/: {', '.join(files)}")
        print("next: fill in the TODOs, then  python -m birddog.sources validate " + args.name)
        return 0

    if args.cmd in ("list", "validate"):
        folders = discover(root)
        if args.cmd == "validate" and args.names:
            folders = [root / n for n in args.names]
        bad = 0
        for folder in folders:
            try:
                spec = load_spec(folder)
                state = "ok" if spec.enabled else "ok (disabled)"
                print(f"{folder.name:30} {state:15} {spec.fetch['type']:13} "
                      f"schedule={spec.schedule!r} -> {', '.join(spec.produces)}")
            except SpecError as e:
                bad += 1
                print(f"{folder.name:30} INVALID")
                for problem in e.problems:
                    print(f"    - {problem}")
        if not folders:
            print(f"no sources in {root}")
        return 1 if bad else 0

    folder = root / args.name
    spec = load_spec(folder)
    if args.cmd == "fetch":
        from birddog.sources.fetchers import FetchContext, resolve
        ctx = FetchContext(spec.name, spec.folder, dict(spec.fetch),
                           (spec.watermark or {}).get("initial"))
        for rec in itertools.islice(resolve(spec.fetch, spec.folder)(ctx), args.limit):
            print(json.dumps(rec, ensure_ascii=False, default=str))
        return 0

    from birddog.sources import runner
    result = runner.run(spec, steps=tuple(s.strip() for s in args.steps.split(",")),
                        full_refresh=args.full_refresh)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
