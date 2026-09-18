# Measuring the reading pass

`adjudication-set.jsonl` is 35 project pages drawn at random from what a sweep actually queued, each labelled by hand. `adjudicate.mjs eval` rules them with a local model and compares. `adjudicate.mjs rule` refuses to write a ledger until this has been done for the model it is about to use.

## How a page was labelled

The question is the one the model is asked: has the project itself finished?

**finished**: the page states the thing has ended and will not resume. An organisation wound down, a consultation over, a service retired. An intake window closing is not this: registration, applications, submissions, nominations and one round of voting all close on schedule on projects that are running.

**running**: a careful reader would say the page shows the thing operating: a date in the last year or so, a current cycle with its dates on the page, a live content stream, a working service with current data.

**unclear**: neither. An undated brochure page is unclear however active its calls to action sound, because every dormant organisation's page still says Donate. This is the expected answer for a large share of the band and it costs nothing: the sweep's own score stands.

Each row carries a `why` saying what settled it. Label from the `text` stored in the row rather than by opening the site, because that text is exactly what the model is given.

## What the measurement is for

The two errors do not cost the same, so the headline is not accuracy.

Reading a finished project as unclear leaves the listing scored exactly as it is scored today. Nothing is lost that was not already lost.

Reading a live project as finished caps its score at 10, which puts Status at Inactive, and an Inactive record is skipped by every later run. Nothing comes back to re-score it. That error is the one worth counting, and `falseFinished` in `adjudication-eval.json` is where it is counted.

## What this set cannot tell you

One of the 35 pages has finished. The other 34 have not, and nor had the other 11 pages in that sweep's queue, so the rate across all 46 is one in 46.

That is a real finding about the band rather than a gap in the sampling: by the time a page reaches this queue, the wording rules have already found no closure language and the date reader has found no date, and a page that has genuinely finished usually says so in words the rules catch. What is left is mostly live sites the date reader could not get a purchase on.

So the set measures one half of the question properly and the other half barely at all. **False alarms are measured** on a representative sample, which is the half that costs something. **Recall rests on a single page**, so `caughtFinished` is a number out of one and moves in steps of 100%. Read it as an anecdote, not a rate.

Measuring recall needs a second set built the other way round, from pages known to have finished. Do not build it by hand-picking out of this queue, which holds one. Let the queue accumulate across sweeps and draw again, or assemble a set from listings a curator has already ruled Inactive with a closure statement on the page.

Until that exists, read the eval as: on the pages this pass will actually see, how often does it propose retiring something that is alive.

## What was measured

`openai/gpt-oss-120b`, one instance, 32,768-token window, 2026-09-17.

|  | said finished | said running | said unclear |
| --- | --- | --- | --- |
| **is finished** | 0 | 0 | 1 |
| **is running** | 0 | 22 | 4 |
| **is unclear** | 0 | 4 | 4 |

Agreed with the label on 26 of 35. Said "finished" zero times, so zero false alarms. Nothing failed to parse or send.

Eight of the nine disagreements are along the running/unclear boundary, which is the soft half of the labelling and changes no score in either direction. Four undated pages it called running where the label says unclear, and four pages with a date on them it called unclear.

The ninth is the only finished page in the set, and it was read as unclear. That is the cheap direction to be wrong in, because the listing keeps the score it already had, but it is the whole of the recall measurement and it reads 0 of 1.

Every label was checked against the whole stored page, not the opening of it. Twenty of the 35 pages run past the first few thousand characters, and the tail is what settles one of them: Mapaton Ciudadano carries "#MapatonCiudadano 2016 - 2018" in its footer, which is well past where a quick read stops.

## One number to treat with suspicion

Zero false alarms is one sample, not a property of the model.

Mapaton Ciudadano is the page that shows why. With the same model, the same temperature of 0 and the same stored text, it has been read both ways: the full queue run of 2026-09-16 returned finished, the eval minutes earlier returned unclear, five single-page re-runs afterwards returned unclear five times out of five, and the eval re-run of 2026-09-17 returned unclear again.

So the verdict on a borderline page is not stable across runs. The likely cause is that a request is not evaluated in isolation on a server batching concurrent requests, and this is a mixture-of-experts model, where what else is in the batch can change the path a token takes. It is not a parsing fault and it is not the model inventing evidence: the words it quoted are genuinely on that page.

The label here was `unclear` until 2026-09-16, on the reasoning that a year range in a footer is not the page saying it ended. The curator ruled the other way and the record was written Inactive, so the label is now `finished` and the set is scored against the decision that was actually made. The run that looked like the outlier was the one that got it right.

What follows from it: a single eval measures the distribution at one point, a page near the boundary can fall either way on any given run, and the review step in `apply_adjudications.py` is not a formality. It is the only thing standing between a run-to-run coin flip and a listing being retired for good.
