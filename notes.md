# Working notes

Running log of things that surprised me. These become the README and the
interview answers later. Add a line whenever something is not what I expected.

---

**2026-08-16, first look at the API**

The match list endpoint already has the seed, both players' elo, and the result.
I expected to need a second request per match. Only the event timelines need
that, and those are for the win probability model later.

History goes back further than I thought. Match id 1,000,000 is June 2024,
season 5. Id 100,000 is gone, so something got purged or ids did not start at 1.

Only about 44% of matches are ranked (type 2). Another chunk are private or solo
runs that carry no elo. So the raw match count overstates how much usable data
there is.

Of ranked matches, more than half are forfeited. In a 700 match sample: 309
ranked, of which only 127 finished. That is the real bottleneck, not the API
rate limit.

`match_type` is not in the docs. Worked it out by checking which types always
carry elo changes. Type 2 does, always. Types 1, 3 and 4 never do.

Seed types are not evenly distributed. In a 700 match sample VILLAGE came up 177
times and JUNGLE_TEMPLE only 3. Whatever model I build will barely see jungle
temples, and I should not pretend otherwise in the results.

37 of 700 matches had no seed data at all. Have not looked at why yet.

---

**2026-08-16, nearly shipped a bad model**

Wondered whether to skip low elo matches at collection time, on the grounds that
they are slow, mostly forfeited, and full of implausible times like a 1100 elo
player apparently finishing in under six minutes.

Two things came out of checking instead of assuming.

The implausible times are not players. `result_time` is filled in on forfeited
matches too, and on those it is when the match *ended*, not when anyone
finished. The detail endpoint proves it: forfeited matches have an empty
`completions` array. So a 1117 elo pair showing 5:45 is an opponent quitting
after five minutes, credited to the winner as if it were a run. Low elo looked
anomalous because low elo forfeits more, 74.7% under 900 versus 19.3% above
2100, so the bug concentrated there. Filtering by elo would have hidden it
rather than fixed it, and I would have shipped a model trained on invented runs.

And the low end turns out to be the interesting part. Seed effects computed
separately under and over 1200 elo correlate at r = 0.89, so the low bracket
carries the same signal. But the effects are two to three times larger down
there. Ruined portal is worth 9.2% under 1200 and 3.4% above it. Good seeds
help weak players more.

**This last paragraph is wrong. See the next entry. Leaving it here because the
mistake is the interesting part.**

General lesson I want to remember: filter on things that were true before the
seed was drawn, never on things the seed could have caused. Elo is safe, run
time is not, and "looks like an outlier" is run time wearing a hat.

Also: the API supports `type=2` server side, so the collector now asks for
ranked only. 2.25x fewer requests per usable match. `eloMin` is not supported,
it gets silently ignored, which is worth knowing before trusting any other
undocumented parameter.

55% of ranked matches are forfeited. That is the real data constraint.

---

**2026-08-16, the interaction was not real**

Noticed buried treasure was missing from my seed effect table. Two bugs behind
that, one cosmetic and one that killed the headline result.

Cosmetic one: I lined up the low and high elo columns and dropped rows missing
from either. Buried treasure has no low elo rows, so it vanished from both
columns instead of showing up in one.

The real one: seed types are not handed out uniformly. The game assigns them by
rank. Buried treasure is 1200+ only, ruined portal is 600+ only, and villages
are 55% of seeds at the bottom versus 20% at the top. My data matches the wiki
table to within a percent.

So seed type partly encodes skill. Ruined portal below 1200 elo was largely a
marker for being above 600, where it appears 18.5% of the time against 0.6%
below. The -9.2% I measured was mostly the skill difference between those two
groups wearing a seed type as a disguise.

Redid it comparing runs only against other runs in the same 100 point elo bin.
Ruined portal is -3.4% at 600-1199 and -3.6% at 1200+. The interaction is gone.
The seed effect is basically flat across skill.

Worse result, and I liked the old one more, which is exactly why it needed
checking. The tell was there in the data the whole time: a seed type with zero
rows in one bracket should have made me ask why before I trusted any comparison
across brackets.

Rule for the rest of this project: bin on elo before comparing seed types, or
just work inside 1200+ where the distribution is actually uniform and assignment
is genuinely random.

## the same column three times

523k matches now, 244k finished. The elo controlled table barely moved, it just
got precise: intervals from +/- 0.9 down to +/- 0.2. Village and desert temple
separated from zero, buried treasure still did not.

First real model. Target log(minutes), elo as a covariate instead of binning,
train/test grouped by seed_id because 523k matches only use 355k seeds and I do
not want a seed and its twin sitting on opposite sides of the split.

Then the linear coefficients came out with ruined portal at +6.0%. The binned
estimate says -4.4%. Same data, opposite sign.

It is not a bug. type:structure:lava and type:structure:completable only ever
occur on ruined portal seeds, and between them they cover 48,954 of the 48,957.
The dummy and those two tags are the same column written three ways. Ridge has
one effect to hand out and three identical places to put it, so it splits it
however the penalty prefers, and each piece can land anywhere including the
wrong side of zero.

The sum is what is real. Add up every seed coefficient that applies to a match,
average within seed type, and it lands at -5.2% against the binned -4.4%. All
five seed types line up in the same order across both methods.

Second time now that a number looked exciting and turned out to be an artifact
of how the data is put together rather than a fact about Minecraft. Both times
the check was cheap and I only ran it because the number disagreed with
something I already believed.

The actual headline is dull. Elo alone predicts a run to 2.97 minutes of error.
Every seed feature I have buys 4 more seconds. The seed is real, it is about 5%,
and it is nowhere near the size of the player.
