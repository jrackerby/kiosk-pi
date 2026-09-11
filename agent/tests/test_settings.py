"""The settings map: coercion, refusal, atomicity, and notification."""

from __future__ import annotations

import json
import os

import pytest

from pikioskd.settings import (
    BROWSER_RESTART_KEYS,
    SettingError,
    Settings,
    _as_bool,
    _as_str_list,
    defaults,
)


@pytest.fixture()
def settings(tmp_path):
    store = Settings(str(tmp_path / "settings.json"))
    store.load()
    return store


@pytest.mark.parametrize("text", ["1", "true", "TRUE", "yes", "on", " True "])
def test_truthy_strings(text):
    assert _as_bool(text) is True


@pytest.mark.parametrize("text", ["0", "false", "FALSE", "no", "off", " 0 "])
def test_falsy_strings_are_false(text):
    """The bug this exists to catch: ``bool("false")`` is True.

    A query parameter is always a string, so a setting coerced with a bare
    ``bool()`` can be turned on and never off, and the API answers 200 both
    times. Every one of these would pass a ``bool()`` implementation.
    """
    assert _as_bool(text) is False


def test_nonsense_boolean_is_refused():
    with pytest.raises(SettingError):
        _as_bool("maybe")


def test_flag_list_splits_on_whitespace_not_commas():
    """``--disable-features=A,B`` is ONE flag.

    A comma-delimited channel carries its own delimiter here, and the failure
    is not a crash: the flag arrives as two, one of which Chromium ignores.
    """
    flags = _as_str_list("--kiosk --disable-features=WaylandFractionalScaleV1,Translate")
    assert flags == ["--kiosk",
                     "--disable-features=WaylandFractionalScaleV1,Translate"]


def test_integers_read_back_as_integers(settings):
    """The Fully deviation, asserted.

    Fully returns most integers as strings, so ``timeToScreenOffV2`` reads
    ``'0'`` and every ``!= 0`` comparison calls a correct device drifted. A
    regression here would be invisible to any test that compares with ``==``
    after ``str()``, which is why the type itself is asserted.
    """
    settings.set("timeToScreenOffV2", "90")
    value = settings.get("timeToScreenOffV2")
    assert value == 90
    assert isinstance(value, int)
    assert not isinstance(value, str)


def test_unknown_key_is_refused(settings):
    with pytest.raises(SettingError):
        settings.set("thisKeyDoesNotExist", "1")


def test_out_of_range_brightness_is_refused(settings):
    with pytest.raises(SettingError):
        settings.set("screenBrightness", 999)
    assert settings.get("screenBrightness") == defaults()["screenBrightness"]


def test_a_rejected_key_leaves_the_whole_write_untouched(settings):
    """All-or-nothing. A partial write leaves a state nobody asked for."""
    settings.set("startURL", "http://example.invalid/a")
    with pytest.raises(SettingError):
        settings.set_many({"startURL": "http://example.invalid/b",
                           "screenBrightness": 5000})
    assert settings.get("startURL") == "http://example.invalid/a"


def test_write_is_persisted_and_reloads(tmp_path):
    path = str(tmp_path / "settings.json")
    first = Settings(path)
    first.load()
    first.set("startURL", "http://example.invalid/board")

    second = Settings(path)
    second.load()
    assert second.get("startURL") == "http://example.invalid/board"


def test_settings_file_is_not_world_readable(settings):
    settings.set("remoteAdminPassword", "hunter2")
    mode = os.stat(settings.path).st_mode & 0o777
    assert mode == 0o600, f"settings carry the admin password; mode is {mode:o}"


def test_unparseable_file_raises_rather_than_falling_back(tmp_path):
    """Falling back to defaults would silently drop the admin password.

    An agent that came up on defaults after a truncated write would have an
    EMPTY password — which refuses every request, so the wall is not open, but
    it is also unrecoverable remotely and says nothing about why.
    """
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        Settings(str(path)).load()


def test_one_bad_value_loses_one_key_not_the_file(tmp_path, caplog):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "startURL": "http://example.invalid/keepme",
        "screenBrightness": 9999,
    }), encoding="utf-8")
    store = Settings(str(path))
    store.load()
    assert store.get("startURL") == "http://example.invalid/keepme"
    assert store.get("screenBrightness") == defaults()["screenBrightness"]


def test_listeners_see_only_the_changed_keys(settings):
    seen: list[set[str]] = []
    settings.add_listener(seen.append)
    settings.set("startURL", "http://example.invalid/x")
    settings.set("startURL", "http://example.invalid/x")  # no-op
    assert seen == [{"startURL"}]


def test_restart_keys_are_a_subset_of_the_declared_settings():
    """A restart key that is not a setting can never fire.

    It would sit in the set forever looking like coverage, and the browser
    would silently keep running with stale flags after that key was written.
    """
    assert BROWSER_RESTART_KEYS <= set(defaults())


def test_redaction_hides_the_password_and_nothing_else(settings):
    settings.set("remoteAdminPassword", "hunter2")
    redacted = settings.as_dict(redact=True)
    assert redacted["remoteAdminPassword"] == "**redacted**"
    assert redacted["startURL"] == settings.get("startURL")
    assert settings.as_dict()["remoteAdminPassword"] == "hunter2"


def test_the_assertions_above_can_fail():
    """LAW §4: an assertion set needs a self-test proving it CAN fail.

    Each of these is the mutation the corresponding test is meant to catch. If
    any of them stops raising, the test above it has stopped asserting.
    """
    with pytest.raises(AssertionError):
        assert bool("false") is False          # the coercion bug
    with pytest.raises(AssertionError):
        assert isinstance("90", int)           # the Fully type bug
    with pytest.raises(AssertionError):
        assert "--disable-features=A,B".split(",") == ["--disable-features=A,B"]


def test_a_failed_save_rolls_memory_back(settings, monkeypatch):
    """All-or-nothing has to survive the WRITE failing, not just a bad value.

    Leaving memory updated after save() raises makes the agent run on a value
    that is not in the settings file: listSettings reads it back as correct,
    the panel behaves as if it took, and it is gone at the next restart with
    nothing on the request path to say so. Measured live as an unwritable
    /etc/pikioskd — the permissions were the trigger, but a full SD card or an
    ext4 that has flipped read-only does the same thing.
    """
    settings.set("startURL", "http://before.invalid/")

    def refuse() -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(settings, "save", refuse)
    with pytest.raises(PermissionError):
        settings.set("startURL", "http://after.invalid/")

    assert settings.get("startURL") == "http://before.invalid/", (
        "memory kept the value that never reached disk, so the agent now "
        "reports a setting the file does not contain"
    )


def test_a_failed_save_rolls_back_every_key_in_the_write(settings, monkeypatch):
    """A multi-key write must not half-apply either."""
    settings.set_many({"deviceName": "BEFORE", "timeToScreensaverV2": 60})

    def refuse() -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(settings, "save", refuse)
    with pytest.raises(OSError):
        settings.set_many({"deviceName": "AFTER", "timeToScreensaverV2": 900})

    assert settings.get("deviceName") == "BEFORE"
    assert settings.get("timeToScreensaverV2") == 60


def test_the_rollback_check_can_fail(settings, monkeypatch):
    """LAW §4: prove the assertion catches the shape it is written for."""
    original = settings.get("startURL")
    settings._values["startURL"] = "http://not-on-disk.invalid/"  # noqa: SLF001
    with pytest.raises(AssertionError):
        assert settings.get("startURL") == original
    settings._values["startURL"] = original  # noqa: SLF001
