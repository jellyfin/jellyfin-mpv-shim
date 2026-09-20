"""Writing catalog columns that the app itself may not write.

`SyncDB.update` takes an allow-list -- five columns, measured as every
keyword any caller in the package passes -- so that nothing in the app can
set an identity column through that door, however it is called. Fixtures
still need to *construct* states the app arrives at by other routes (a
manifest that will not parse, a library id resolved at download time, a row
homed to a server), and going round the allow-list from a test is not the
same act as going round it from production.

Raw SQL rather than a private `SyncDB` method, deliberately: a method on the
store is reachable from the app, and then the allow-list is a suggestion.
"""


def set_columns(db, item_id, **fields):
    """Write `fields` onto one downloads row, allow-list and all.

    Column names are interpolated, so they must be literals in test code --
    never a value under test. The values themselves are bound.
    """
    if not fields:
        return
    assignments = ",".join("%s=?" % name for name in fields)
    with db._lock:
        db._conn.execute(
            "UPDATE downloads SET %s WHERE item_id=?" % assignments,
            list(fields.values()) + [item_id])
        db._conn.commit()
