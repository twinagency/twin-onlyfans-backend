"""Additive runtime migration. Rollback retains schedules and delivery evidence.

Run with the established explicit URL selector before starting the new sender:
python -m native_pilot.pacing_schema --url-env ONLYFANS_MIGRATION_URL
"""
import argparse
import os

from twin_bridge import store
from .pacing import DDL, INDEX


def upgrade(target):
    with store.transaction(target) as con:
        # search_path is the existing fastt_runtime,public; never create a public
        # scheduling table if the independent native schema has not been installed.
        if store.is_pg(target) and not con.execute("SELECT to_regnamespace('fastt_runtime')").fetchone()[0]:
            raise RuntimeError("native runtime schema required")
        con.execute(DDL)
        con.execute(INDEX)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-env", required=True)
    args = parser.parse_args()
    upgrade(os.environ[args.url_env])


if __name__ == "__main__":
    main()
