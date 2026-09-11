"""The gap list, gated by the only thing that will ever gate it.

`hassfest` NEVER CHECKS A CUSTOM COMPONENT AGAINST THE QUALITY SCALE.
`validate_iqs_file` opens with `if not integration.core: return`, so
`quality_scale.yaml` goes unread — while `manifest.json`'s schema still ACCEPTS
a `quality_scale` key. A tier declared there is a self-claim with no gate behind
it, green for ever.

THAT IS NOT THEORETICAL: the file shipped in 1.0.0 was not valid YAML at all. A
plain scalar carrying a colon ran the parse off the rails at the `diagnostics`
rule, and every tool in the repository stayed green over it for the file's whole
life, because no tool ever opened it. A document nobody parses cannot be wrong,
which is the failure mode, not the excuse.

So the assertions here are the ones that would have caught that: the file
parses, its keys are the shape the scale defines, and the tier the manifest
CLAIMS is one the file itself substantiates. What this cannot do is check the
rule NAMES against `ALL_RULES` in home-assistant/core — that list moves, and
pinning a copy of it here would be exactly the remembered summary the standard
says not to read from. It is re-read and re-joined by hand when this file is
edited; the join for the current contents is in this commit's message.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

COMPONENT = Path(__file__).parent.parent / "custom_components" / "kiosk_pi"

VALID_STATUSES = {"done", "todo", "exempt"}

# The tiers are cumulative: claiming silver claims bronze with it.
TIER_ORDER = ("bronze", "silver", "gold", "platinum")


@pytest.fixture(scope="module")
def gap_list() -> dict:
    return yaml.safe_load((COMPONENT / "quality_scale.yaml").read_text())


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((COMPONENT / "manifest.json").read_text())


def test_the_gap_list_is_valid_yaml(gap_list) -> None:
    """The one that 1.0.0 failed. Nothing else in this repository opens it."""
    assert isinstance(gap_list, dict)
    assert isinstance(gap_list.get("rules"), dict)
    assert gap_list["rules"], "a gap list with no rules in it is not a gap list"


def test_every_rule_carries_a_status_the_scale_defines(gap_list) -> None:
    """A typo'd status is indistinguishable from a passing one to a reader
    skimming for `todo`, and there is no validator to say otherwise."""
    for name, entry in gap_list["rules"].items():
        status = entry.get("status") if isinstance(entry, dict) else entry
        assert status in VALID_STATUSES, f"{name}: {status!r}"


def test_a_deviation_states_its_reason(gap_list) -> None:
    """An exemption is a RULING and a ruling carries a citation.

    A rule skipped quietly is a rule nobody can find later, and the next
    session re-derives the argument from nothing.
    """
    for name, entry in gap_list["rules"].items():
        if isinstance(entry, dict) and entry.get("status") == "exempt":
            assert entry.get("comment", "").strip(), \
                f"{name} is exempt with no reason recorded"


def test_the_manifest_does_not_claim_a_tier_the_gap_list_refuses(
    gap_list, manifest
) -> None:
    """THE SELF-CLAIM, GATED. The manifest's tier is the only number a HACS
    user or a reviewer ever sees, and hassfest will not check it here.

    Asserted as "no rule is outstanding at or below the claimed tier" using the
    file's own section ordering: rules are listed tier by tier, so everything
    above the last `done`/`exempt` block belongs to a tier not yet claimed. A
    `todo` at or under the claimed tier means the manifest is overstating.
    """
    claimed = manifest["quality_scale"]
    assert claimed in TIER_ORDER, claimed

    text = (COMPONENT / "quality_scale.yaml").read_text()
    # The file marks its tiers with banner comments; find where the claimed
    # tier's block ends, and hold everything before that to done/exempt.
    banners = {
        tier: text.lower().index(f"--- {tier}")
        for tier in TIER_ORDER if f"--- {tier}" in text.lower()
    }
    assert claimed in banners, f"no {claimed} section in the gap list"

    following = [banners[t] for t in TIER_ORDER
                 if t in banners and banners[t] > banners[claimed]]
    cutoff = min(following) if following else len(text)

    outstanding = [
        name for name, entry in gap_list["rules"].items()
        if isinstance(entry, dict) and entry.get("status") == "todo"
        and text.index(f"\n  {name}:") < cutoff
    ]
    assert not outstanding, (
        f"manifest claims {claimed} while these are still todo: {outstanding}"
    )
