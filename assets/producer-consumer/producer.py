"""Example service; adaptations must import their project's actual serializer."""
import json
import sqlite3


def serialize_preview(database: sqlite3.Connection) -> str:
    count = database.execute('SELECT COUNT(*) FROM stations').fetchone()[0]
    return json.dumps({'kind': 'preview', 'station_count': count})
