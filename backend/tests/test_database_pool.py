def test_database_engine_guards_against_stale_connections(app):
    from database.database import engine

    assert engine.pool._pre_ping is True
    assert engine.pool._recycle == 1800
