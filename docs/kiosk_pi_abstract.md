# kiosk_pi — abstract

Sibling: `kiosk_pi_patent_disclosure.md` (a patent-disclosure draft).
The technical reference lives with the integration, `jrackerby/kiosk-pi`;
the 0.7.0-era architecture, data-flow and process-flow documents that used
to sit beside this one described a transport 1.0.0 no longer has and were
retired (GH-734).

## The problem

A handful of small wall-mounted screens around the house each run a single
web page, permanently, with nobody sitting in front of them to notice if
something goes wrong. That is the whole point of a wall display — it should
just be there, quietly showing the right thing, forever. But "quietly
showing the right thing" is a surprisingly fragile state. The little
computer behind the glass can overheat, run low on power, lose its network,
run out of storage, or simply crash the program that draws the screen — and
because nobody is watching it the way they'd watch a laptop, none of that
announces itself. A wall display that has silently frozen, gone dark, or
wandered off to a login screen looks, from six feet away, exactly like one
that is working.

There is a second, quieter version of the same problem: even when a display
is technically alive, it can be pointed at the wrong thing. Someone
reorganizes what these screens are supposed to show, and one wall keeps
displaying an older page that no longer exists or no longer matters. A
screen that is up and running but showing yesterday's information is not
much better than one that is off.

And there is a third version, about who can walk up and use the screen.
Some of these panels are meant to be touched and used by a specific person
in the household; others are meant to be a display only, for anyone to
glance at. Getting that assignment wrong — a display panel that quietly
lets in more than intended, or a control panel nobody can actually use —
is the kind of mistake that is invisible until someone is standing in front
of the screen confused.

## What this does about it

This is the system that keeps watch over that little fleet of wall
computers on the household's behalf, so a person doesn't have to walk up to
each one to find out if it's healthy. It checks in on every screen
regularly — its temperature, its power, its storage, whether the display
program is still actually running, and, critically, what web page it is
genuinely showing right now versus what it was told to show. Those last two
can quietly drift apart, and catching that gap is one of the more important
things this system does, because a screen that looks fine from a photograph
can still be sitting on the wrong page.

It also tells the difference between a screen that is broken and a screen
that is deliberately turned off. A powered-down display that was switched
off on purpose should never look like a crisis, and one that genuinely went
dark unexpectedly should never be mistaken for "someone probably just turned
it off." Collapsing those two into one signal is exactly the kind of mistake
that trains a person to stop trusting the warning light altogether, so this
system is built to keep them separate.

Beyond watching, it offers a small set of safe remote actions: turning a
screen's picture on or off, restarting the software that draws it,
clearing out its temporary files, sending it back to its assigned page, and
— only where explicitly allowed, one screen at a time — applying software
updates. Every one of those actions checks its own result afterward rather
than assuming it worked, because a command that reports success while
quietly changing nothing is worse than one that visibly fails.

Finally, it keeps a light paper trail on who each screen is meant to be
used by, and checks that declaration against the household's actual access
rules and against what the screen itself is configured to allow — not to
control who gets in, but so a mismatch between "who this is for" and "who
can actually use it" shows up as a finding rather than as a surprise the
next time someone stands in front of the glass.

## What this buys a household member

Nobody has to physically check each wall screen to know it is alive, on the
right page, and safe to leave alone. A display that has quietly drifted,
overheated, or gone dark reads as a finding rather than staying invisible
until someone happens to notice by eye — and a display that was
deliberately turned off never gets confused with one that broke.
