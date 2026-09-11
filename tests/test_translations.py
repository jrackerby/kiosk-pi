"""`strings.json` and `translations/en.json` must not drift apart.

THIS FILE EXISTS BECAUSE THEY DID. The `upgraded` abort reason added with the
in-place upgrade landed in `translations/en.json` alone, and every suite stayed
green: hassfest does not compare the two for a custom component, and nothing
else reads `strings.json` at runtime. The failure would have surfaced in a
future translation as a missing key nobody could trace back to this commit.

`strings.json` is the SOURCE — what hassfest validates and what every other
language is derived from — and `translations/en.json` is its English rendering.
For an integration whose only shipped language is English the two are the same
document, so any divergence is a mistake rather than a translation.
"""

from __future__ import annotations

import json
import pathlib

import pytest

COMPONENT = (pathlib.Path(__file__).resolve().parents[1]
             / "custom_components" / "kiosk_pi")
STRINGS = COMPONENT / "strings.json"
ENGLISH = COMPONENT / "translations" / "en.json"


def _flatten(value, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            out.update(_flatten(item, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = item
    return out


def test_both_files_exist_and_parse():
    """A sweep that cannot read its inputs is not evidence of agreement."""
    for path in (STRINGS, ENGLISH):
        assert path.is_file(), f"{path} is missing"
        assert json.loads(path.read_text(encoding="utf-8"))


def test_the_two_files_carry_the_same_keys():
    source = _flatten(json.loads(STRINGS.read_text(encoding="utf-8")))
    english = _flatten(json.loads(ENGLISH.read_text(encoding="utf-8")))
    missing = sorted(set(source) - set(english))
    extra = sorted(set(english) - set(source))
    assert not missing and not extra, (
        f"strings.json and translations/en.json have drifted.\n"
        f"  only in strings.json: {missing}\n"
        f"  only in en.json:      {extra}\n"
        "strings.json is the source; add the key there too."
    )


def test_the_two_files_carry_the_same_text():
    source = _flatten(json.loads(STRINGS.read_text(encoding="utf-8")))
    english = _flatten(json.loads(ENGLISH.read_text(encoding="utf-8")))
    differing = sorted(k for k in set(source) & set(english)
                       if source[k] != english[k])
    assert not differing, f"same key, different text: {differing}"


def test_every_abort_reason_the_flow_can_return_has_a_string():
    """An abort with no string renders its raw key to the operator.

    Read from the SOURCE rather than from a list kept here — a hardcoded list
    would keep passing after a new reason was added, which is the exact way the
    `upgraded` key got in unnoticed.
    """
    flow = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    reasons = set()
    for marker in ('async_abort(reason="', 'reason="'):
        start = 0
        while (index := flow.find(marker, start)) != -1:
            rest = flow[index + len(marker):]
            reasons.add(rest[:rest.index('"')])
            start = index + len(marker)

    strings = json.loads(STRINGS.read_text(encoding="utf-8"))
    declared = set((strings.get("config") or {}).get("abort", {}))
    declared |= set((strings.get("options") or {}).get("abort", {}))
    # Home Assistant supplies these itself; an integration need not.
    builtin = {"reauth_successful", "already_configured", "reconfigure_successful"}
    missing = sorted(reasons - declared - builtin)
    assert not missing, (
        f"config_flow.py can abort with {missing}, which strings.json does not "
        "define — the operator would see the raw key"
    )


def test_the_drift_check_can_fail(tmp_path):
    """Self-test: prove the comparison detects a missing key."""
    a = _flatten({"config": {"abort": {"x": "1", "y": "2"}}})
    b = _flatten({"config": {"abort": {"x": "1"}}})
    assert sorted(set(a) - set(b)) == ["config.abort.y"]
    with pytest.raises(AssertionError):
        assert not (set(a) - set(b))
