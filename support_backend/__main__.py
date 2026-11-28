"""Module CLI entrypoint (`python -m support_backend ...`) without runpy import warnings."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .knowledge import main as knowledge_main
from .presets import export_dynamic_presets


def _export_presets_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m support_backend export-presets")
    parser.add_argument(
        "--from",
        dest="dynamic_dir",
        default="/var/lib/private/wechat-relay/presets",
        help="Runtime dynamic presets directory",
    )
    parser.add_argument(
        "--to",
        dest="target_dir",
        default="rag-poc/corpus",
        help="Target Git corpus directory",
    )
    args = parser.parse_args(argv)
    written = export_dynamic_presets(Path(args.dynamic_dir), Path(args.target_dir))
    print(f"Exported {len(written)} preset file(s) to {args.target_dir}")
    for path in written:
        print(f"  - {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "export-presets":
        return _export_presets_cli(args[1:])
    return knowledge_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
