# Retries, breakers, and the capacity that decides whether they help

Three questions, each with a measured answer, and two of the three answers are
not the ones the usual advice gives.

Every number here comes from `amg resilience`, and is recorded in
`reports/evaluation.md`. The two figures the front page quotes are also written
into `examples/baseline.json` and re-measured by the gate, because a number on a
front page that nothing re-measures is a number that will eventually be wrong.

## Everything is measured under load, and that is the point

There are two ways to serve a corpus here:

* **sequential** — every request in its own empty simulation. No queue, no
  contention. This is what a benchmark script measures when nobody has thought
  about load, and it contains no resilience information at all;
* **loaded** — requests arrive at a rate against a bounded pool of concurrent
  slots.

Retries are a positive feedback loop, and the loop only exists in the second
mode: a failure produces another call, another call takes a slot from a bounded
pool, a fuller pool makes fresh requests queue, and a request that queues past
its deadline is another failure. `examples/quickstart.py` runs the same three
policies in both modes so the gap is visible in one screen — `best` answers
99.33% sequentially and 18.96% at 40 requests a second against 24 slots.

## An end-to-end deadline, not just a per-attempt timeout

This distinction cost a day of debugging and is the most portable lesson here.

The first implementation had `timeout_us` only: how long one call may take. It
looked complete. Under load it produced a p99 of **9,023 ms against a 6,000 ms
budget**, with the error rate flat.

The reason is that **a per-attempt timeout starts after the queue**. A request
that waits three seconds for a slot and then runs a two-second call took five
seconds, and no timeout was ever exceeded. With unbounded queueing, latency grows
without bound while every error-rate dashboard stays green.

So `GatewayConfig` carries `deadline_us` as well, the clock starts when the
request arrives, and a request still queued when its deadline passes abandons
its place and returns `expired`. The p99 then pins to the deadline, which is
what a latency budget is supposed to mean. `Settings` refuses to start if
`AMG_DEADLINE_US` is below `AMG_TIMEOUT_US`, because no complete attempt could
ever fit inside it.

## 1. Does retrying help?

Yes, and less as load rises.

| Independent failure rate | single | retry | at 24 slots |
| --- | --- | --- | --- |
| 10% | 98.12% | 99.96% | +1.84p |
| 20% | 94.71% | 99.46% | +4.75p |
| 40% | 60.33% | 83.83% | +23.50p |
| 60% | 39.58% | 61.67% | +22.09p |

At 96 slots the same 40% row reads 82.25% to 96.54%. The retry is worth more
when there is capacity to absorb the extra calls it makes — which is the same
observation as the third finding, seen from a different angle.

## 2. Does a circuit breaker help against a high error rate?

**Measured: no.** It is a small, consistent loss.

| Independent failure rate | retry | retry+breaker | breaker's contribution |
| --- | --- | --- | --- |
| 20% | 99.46% | 99.46% | 0.00p |
| 40% | 83.83% | 83.62% | **-0.21p** |
| 60% | 61.67% | 57.21% | **-4.46p** |

A consecutive-failure breaker is a detector for an upstream being **down**, not
for one being **flaky**. Against elevated *independent* errors it trips by
coincidence — some run of failures is bound to happen — and then sheds traffic
that would have succeeded. Configuring a breaker against an error-rate SLO is a
category error, and this is the sweep that shows it.

## 3. Does a breaker help against an outage?

**Only if the fallback has capacity**, and that turns out to be the whole story.

Under a **total** outage of the cheapest provider, with the others healthy:

| Pool | single | retry | retry+breaker |
| --- | --- | --- | --- |
| 24 slots | 3.54% | 2.12% | **21.00%** |
| 96 slots | 98.00% | 99.25% | **99.62%** |

The breaker's *advantage* is far larger on the small pool — +18.88 points versus
+0.37 — which reads as an argument for breakers right up until the absolute
numbers are put beside it. **The same breaker serves a fifth of the traffic on a
pool sized for the fast provider and almost all of it on one sized for the
slow.** Capacity is worth several times what the breaker is worth, and no
breaker setting recovers a pool that cannot absorb the failover.

The mechanism is the latency ratio, not the error rate. The breaker's job is to
stop calling a dead provider and start calling a live one, which moves the
entire load onto the live one. The live one is six or thirty times slower, so
the same arrival rate now needs six or thirty times the concurrency. A gateway
sized for the fast path does not have it, so **the failure looks like latency,
not like an outage** — and the dashboards that would have caught it are watching
error rates.

### The breaker can be actively harmful

During a *partial* outage on the small pool it costs up to **-38.71 points**
against plain retries:

| Outage duty cycle | retry | retry+breaker | breaker's contribution |
| --- | --- | --- | --- |
| 10% | 99.83% | 74.42% | **-25.41p** |
| 25% | 95.62% | 74.42% | **-21.20p** |
| 50% | 79.62% | 40.92% | **-38.71p** |

Waiting out a provider that is briefly unavailable beats shedding its traffic
onto one that cannot absorb it. A breaker is not a free safety net to switch on.

## Two artefacts this sweep had to be rewritten to avoid

Both are worth naming, because both produced a confident result that was a fact
about the harness rather than about breakers.

**The outage applied to every provider.** In the first version the fault model
took all three upstreams down together, so failover had nowhere to go and the
breaker looked useless. That is not a finding about breakers; it is a finding
about a sweep with no survivor. The outage now hits one provider.

**The breaker wedged itself permanently.** A half-open probe granted permission,
but the call it authorised was never made — the request's deadline expired while
it was still queued — so nothing ever reported back, `probe_in_flight` stayed
set, and every subsequent call was rejected for the rest of the run. That is a
real bug, in a shape real breaker implementations have: a permission granted on
one path and released on another, where one of those paths can be abandoned.
`CircuitBreaker.release_probe()` exists for it, and the abandoned-slot path
calls it.

## See also

* [The simulator, and its limits](simulator.md) — where the latency ratios come
  from, and why the numbers are what they are.
* [Reproducibility](reproducibility.md) — the virtual clock, and why these
  results are replayable at all.
* `examples/resilience_demo.py` — the third finding, in a couple of minutes.
