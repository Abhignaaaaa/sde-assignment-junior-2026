"""CLI entrypoint for solution package."""

import argparse
import sys

from solution.config import DEFAULT_DB_PATH, DEFAULT_SCHEMAS_DIR
from solution.ingest import ingest_drop
from solution.report import generate_reports


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Assignment A: Interaction Ledger & Reporting Pipeline",
        prog="python -m solution",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # Ingest subcommand
    ingest_parser = subparsers.add_parser(
        "ingest", help="Ingest a drop directory into SQLite ledger"
    )
    ingest_parser.add_argument("drop_dir", help="Path to drop directory (e.g. data/drop_1)")
    ingest_parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database (default: {DEFAULT_DB_PATH})",
    )
    ingest_parser.add_argument(
        "--schemas",
        default=DEFAULT_SCHEMAS_DIR,
        help=f"Path to schemas directory (default: {DEFAULT_SCHEMAS_DIR})",
    )

    # Report subcommand
    report_parser = subparsers.add_parser(
        "report", help="Generate reporting CSVs and data quality summary"
    )
    report_parser.add_argument(
        "--out",
        required=True,
        help="Path to output directory for reports",
    )
    report_parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database (default: {DEFAULT_DB_PATH})",
    )
    report_parser.add_argument(
        "--schemas",
        default=DEFAULT_SCHEMAS_DIR,
        help=f"Path to schemas directory (default: {DEFAULT_SCHEMAS_DIR})",
    )

    return parser


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "ingest":
        ingest_drop(
            drop_dir=args.drop_dir,
            db_path=args.db,
            schemas_dir=args.schemas,
        )
    elif args.subcommand == "report":
        generate_reports(
            out_dir=args.out,
            db_path=args.db,
            schemas_dir=args.schemas,
        )


if __name__ == "__main__":
    main()
