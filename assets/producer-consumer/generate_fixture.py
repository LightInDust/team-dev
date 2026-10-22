"""Seed storage, then export actual serialized responses without remapping keys."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile

from producer import serialize_preview


def generate():
    responses = []
    with tempfile.TemporaryDirectory(prefix='team-dev-contract-db-') as tmp:
        with closing(sqlite3.connect(Path(tmp) / 'fixture.sqlite')) as database:
            with database:
                database.execute('CREATE TABLE stations (id INTEGER PRIMARY KEY)')
                for count in (0, 1, 42):
                    database.execute('DELETE FROM stations')
                    database.executemany('INSERT INTO stations (id) VALUES (?)',
                                         ((i,) for i in range(count)))
                    responses.append(json.loads(serialize_preview(database)))
    return json.dumps(responses, indent=2, ensure_ascii=False) + '\n'


if __name__ == '__main__':
    print(generate(), end='')
