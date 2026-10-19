import pytest

from studio.core.batch_store import _normalize_episode_history


@pytest.mark.parametrize("settings_json", [None, "", "{}", "null", "[]", "{invalid json"])
def test_legacy_optional_settings_decode_to_empty_history(settings_json):
    episode = {"id": "legacy-episode", "settings_json": settings_json}

    _normalize_episode_history(episode)

    assert episode["settings"] == {}
    assert episode["processing_history"] == {}
    assert episode["encoding_elapsed_seconds"] is None


def test_explicit_null_success_record_does_not_crash_or_create_measurements():
    episode = {
        "id": "interrupted-episode",
        "settings_json": (
            '{"processing_history":{"schema_version":1,"attempts":[],'
            '"successful":null,"attempt_count":0}}'
        ),
    }

    _normalize_episode_history(episode)

    assert episode["processing_history"]["successful"] is None
    assert episode["encoding_elapsed_seconds"] is None


def test_valid_successful_rendition_measurement_is_preserved():
    episode = {
        "id": "measured-episode",
        "settings_json": (
            '{"processing_history":{"successful":{"success":true,'
            '"total_transcode_duration_seconds":42.5,"renditions":'
            '{"720p":{"duration_seconds":12.5,"fps":60}}}}}'
        ),
    }

    _normalize_episode_history(episode)

    rendition = episode["processing_history"]["successful"]["renditions"]["720p"]
    assert episode["encoding_elapsed_seconds"] == 42.5
    assert rendition["duration_seconds"] == 12.5
    assert rendition["fps"] == 60
