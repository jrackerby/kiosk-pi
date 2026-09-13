# Invention disclosure draft — kiosk_pi

Sibling: [`kiosk_pi_abstract.md`](kiosk_pi_abstract.md) (what this is for,
no jargon). The technical reference lives with the integration,
`jrackerby/kiosk-pi`.

> **Dated to 0.7.0.** The figures below describe the two-transport design
> (a metrics daemon plus ssh) that 1.0.0 replaced with a single HTTP agent
> (`pikioskd`). The disclosure is kept as the record of what was disclosed
> when, not as a description of the running integration.

> **This is an internal invention-disclosure draft, not a filed patent
> application and not legal advice.** No prior-art search, novelty opinion,
> or freedom-to-operate analysis has been performed. Every specific figure
> is drawn from `custom_components/kiosk_pi/` as read in this session
> (manifest version 0.7.0).

## BACKGROUND

[0001] This integration monitors and remotely controls a small fleet of
wall-mounted single-board computers, each running a fixed dashboard in a
kiosk browser, over two independent transports (a metrics daemon and ssh).
It shares its overall architecture — a polling coordinator that never
raises a failed-update state, disposition-preserving reads, asymmetric
temporal dwells — with `custom_components/household_state`, documented and
disclosed in `household_alert_patent_disclosure.md`. That prior disclosure
already covers: (a) a not-read disposition distinct from a quiescent
reading, (b) asymmetric hysteresis (fast escalation, dwelled
de-escalation), (c) an availability-preserving coordinator that never fails
the whole update, and (d) live-registry device-set membership rather than a
pinned list. This integration instantiates all four of those patterns again
— `coordinator._async_update_data` never raises, `binary_sensor.<host>_health`
dwells on transport misses but trips known-bad readings immediately, and
`select.py`'s board options are discovered live from the Lovelace registry
rather than pinned. **None of that repetition is claimed again here.**
Re-applying an already-disclosed architectural pattern to a second, unrelated
monitoring domain is the routine-engineering case the earlier disclosure's
own claim language already anticipates ("an automated premises-monitoring
system" — this is a second instance of the same system class, not a new
invention).

[0002] What this disclosure evaluates is the material that is specific to
*this* domain — remotely administering an unattended kiosk display over an
untrusted, single-purpose channel — and asks whether any of it goes beyond
routine application of known techniques (state-machine dwelling, anchored
text substitution, two-phase commit) to this particular problem.

## SUMMARY — candidate aspects considered

[0003] **Two-tier, order-committed configuration write with independent
divergence detection (select.py, coordinator.py).** A single logical
"where is this display pointed" value is deliberately maintained in two
places with different persistence properties: a line in a boot-time launch
script (survives a power cycle, does not take effect until the next
restart) and a live command sent to the running browser's remote-debugging
port (takes effect immediately, does not survive a restart). The write path
commits to the persistent copy first, verifies it by reading the file back,
and only then attempts the live copy — explicitly reasoned in source as: a
wall correct after its next reboot and stale until then is preferred over a
wall correct now and silently wrong after the next power cut. A **separate,
independent measurement** (`_track_diverge`) later detects disagreement
between the two copies from independent evidence (the file's own content vs.
a live query to the browser's own reported state), with a short dwell
specifically sized to the single window in which the two-phase write can be
legitimately mid-flight, and resets the streak on any incomparable pair
rather than holding it.

[0004] Ordering a two-phase write by asymmetric persistence properties, and
separately measuring the two copies' convergence with a dwell sized to the
writer's own known transition window, is a specific technique. Whether it
rises above routine two-phase-commit engineering is arguable — two-phase
commit with a "durable-first" ordering is well known in distributed
systems generally. What is more specific to this instance is deriving the
dwell's size from an internal fact about the *writer itself* (both values
are read together in one transport call, so the disagreement window is
provably one poll wide) rather than from an external estimate. This is
judged **routine application of two-phase commit to a two-tier
configuration value**, not independently claimed.

[0005] **Three-way declared-identity audit across disjoint, previously
uncorrelated sources (`sensor.py`'s `KioskPiAuthAuditSensor`).** A device's
intended operator identity is read from a plain-text comment on the remote
host; a household's access-control list is read from the home-automation
platform's own auth-provider configuration; and a per-board display-chrome
policy is read from a separately-loaded dashboard definition. No component
of the system previously compared these three, and each was independently
capable of drifting from the other two without any error being raised by
any of the three systems individually. The audit computes one of six states
via an explicit precedence order, with the read-failure state for the
grant-policy leg (`named is None`, "the board's kiosk_mode block could not
be read") kept structurally distinct from the granted-nobody state
(`named is not None and not named`, "unprotected") — the code's own
history records that collapsing those two once produced an inverted
severity ordering, where the worst case (nobody granted kiosk treatment)
silently scored as a clean pass because the check was gated on the
grant list being non-empty.

[0006] A three-way join across independently-maintained records, with a
"could not determine" state kept structurally separate from a "determined
negative" state for each leg, is the same disposition-preservation
technique already disclosed for `household_state`'s source reads — applied
here to authorization records instead of threat-sensor records. **Judged
the same pattern in a new domain, not independently claimed**, though it is
worth recording that no other component in this system, before this one,
compared these three specific records at all; the *gap it closes* is real
even where the *technique it uses* is not new.

[0007] **Dual-condition "reboot owed" determination combining a
media-state comparison with an install-timestamp/boot-timestamp comparison
(`update.py`'s `_reboot_owed`).** Standard practice compares the running
kernel version against the newest kernel package installed on disk. This
component adds a second, independent sufficient condition: if a remote
package installation was applied and the host's own boot time — read from
`/proc/uptime`, not from the package manager — is earlier than the
recorded application time, a reboot is reported as owed regardless of
whether the pending package was a kernel package at all. The source
records a specific gap this closes: a non-kernel library update (measured,
the Mesa graphics stack) that had already landed on disk left the
kernel-only comparison reporting clean while the already-running browser
process still held the superseded library mapped in memory. The
install-applied timestamp is deliberately persisted outside the normally
volatile in-memory coordinator state specifically because an HA restart
between the remote install and the host's own reboot previously erased the
record and silently reverted the "reboot owed" determination to the
kernel-only comparison.

[0008] Combining two independently-sufficient conditions for one derived
"needs restart" state, where the second condition is not a version
comparison at all but a comparison between a persisted event timestamp and
a freshly-read boot timestamp, is a genuine expansion of what "up to date"
means beyond the conventional kernel/package-version check. It is narrow
and domain-specific (it exists because *this* fleet's exposure is a
long-lived browser process holding shared libraries mapped, not the kernel
itself), but it is not an obvious application of prior art in the way the
two aspects above are — no widely-known technique already frames
"current" as "the running kernel is newest, OR nothing landed since the
last verified reboot," derived from a boot-time read rather than a package
database read.

## CLAIMS

**No claims are asserted.** Of the three aspects evaluated above, two
(the two-tier configuration write and the three-way identity audit) are
judged routine application of known techniques — two-phase commit and
disposition-preserving multi-source joins, respectively — to this
integration's specific domain, and the disposition-preservation technique
itself is already the subject of a claim in `household_alert_patent_disclosure.md`
against a different source integration. The third (the dual-condition
reboot-owed determination) is judged the most distinctive piece of
domain-specific reasoning found in this component, but on the evidence read
this session it is a narrow, single-purpose heuristic bound tightly to one
fleet's specific failure mode (a long-lived browser process holding
superseded shared libraries mapped after a non-kernel package upgrade)
rather than a generalizable method independent of that fact pattern. None
of the three, individually or combined, was judged to clear the bar for an
independent claim without a prior-art search this disclosure does not
perform. Should counsel disagree with the routine-engineering assessment of
[0004]/[0006], those paragraphs identify the two most defensible starting
points.

## ABSTRACT

An integration for remotely monitoring and administering a fleet of
wall-mounted kiosk-browser hosts over two independent transports, sharing
its coordinator architecture with a previously-disclosed threat-monitoring
system. Domain-specific mechanisms considered for novelty include: a
two-tier configuration write ordered by each tier's own persistence
property, with independent divergence detection dwelled to the writer's
own known transition window; a three-way audit joining a host-local
identity declaration, a platform access-control list, and a per-board
display-chrome policy, none of which had previously been compared; and a
dual-condition patch-currency determination that treats an unrebooted
non-kernel package installation as independently sufficient to require a
restart, derived from a boot-timestamp comparison rather than a
package-version comparison. The first two are assessed as routine
application of known techniques to this domain; the third is assessed as
the most domain-specific and least directly anticipated of the three, but
narrow enough in scope that no claim is asserted without further
prior-art review.

---

*Reference implementation: `custom_components/kiosk_pi/` (`coordinator.py`,
`select.py`, `update.py`, `sensor.py`), manifest version 0.7.0, as it stood
in this repo before the integration moved to `jrackerby/kiosk-pi`. See
`jrackerby/HA` `docs/PROCESS.md` for the documentation method used across this repo's
`docs/integrations/` tree.*
