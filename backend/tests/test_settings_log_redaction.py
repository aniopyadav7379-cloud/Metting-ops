"""Credential-like admin settings must never be copied into application logs."""

from __future__ import annotations

import logging


def test_sensitive_setting_value_is_redacted_from_update_log(canonical_db_schema, caplog):
    from services.settings_manager import SettingsManager

    manager = SettingsManager.__new__(SettingsManager)
    manager.cached_settings = {}
    value = "do-not-log-this-integration-secret"

    with caplog.at_level(logging.INFO, logger="services.settings_manager"):
        assert manager.update_setting("integrations.api_key", value) is True

    assert value not in caplog.text
    assert "integrations.api_key = [REDACTED]" in caplog.text


def test_malformed_setting_value_is_not_echoed_to_logs(canonical_db_schema, caplog):
    from database.database import SessionLocal
    from database.models import Settings
    from services.settings_manager import SettingsManager

    manager = SettingsManager.__new__(SettingsManager)
    manager.cached_settings = {}
    value = "not-json-and-not-for-logs"
    db = SessionLocal()
    try:
        setting = db.query(Settings).filter(Settings.key == "network.allowed_origins").first()
        if setting:
            setting.value = value
        else:
            db.add(Settings(
                key="network.allowed_origins",
                value=value,
                category="network",
                description="test malformed setting",
            ))
        db.commit()
    finally:
        db.close()

    with caplog.at_level(logging.WARNING, logger="services.settings_manager"):
        manager.get_all_settings()

    assert value not in caplog.text
    assert "Could not decode JSON setting network.allowed_origins" in caplog.text
