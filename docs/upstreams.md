# Upstreams and cost

## The protocol

Everything the routing and resilience layers do is expressed against one small
protocol: a name, two prices, and a way to answer a task.

```python
class Upstream(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def input_price_per_1k(self) -> int: ...
    @property
    def output_price_per_1k(self) -> int: ...
    def attempt(self, task: Task, *, nonce: int = 0, at_us: int = 0) -> Attempt: ...
```

Two implementations satisfy it: `amg.upstream.simulated`, which every published
number comes from, and `amg.upstream.ollama`, which is how the simulator's table
is checked against real models. They are interchangeable, so a comparison
between them is like-for-like rather than an analogy.

### `Attempt` has no `correct` field

This is the single most load-bearing omission in the codebase.

Correctness is not knowable at request time, and `Attempt` is a record of what
the *gateway* saw. The evaluation joins it against ground truth afterwards.
Putting correctness on the request-time record would let a routing policy read
it by accident, which is the easiest way in the world to write a router that
looks brilliant in evaluation and cannot be deployed.

### Three outcomes, and why `TIMEOUT` is not `ERROR`

`OK`, `ERROR`, `TIMEOUT`. The last two look like they could be one, and folding
them together would break the cost report: **a timed-out call was still served
and still billed.** The money left even though no answer arrived. Merging them
would understate what a retry policy spends, which is precisely the quantity the
resilience sweep exists to measure.

### `nonce` and `at_us`

`attempt()` takes two arguments that look like plumbing and are load-bearing:

* **`nonce`** distinguishes retries of the same task against the same upstream.
  Without it a retry is bit-identical to the attempt that just failed, every
  retry fails too, and the measured value of retrying is exactly zero — a result
  that would look like a finding and be an artefact;
* **`at_us`** is the simulated instant of the call. Only a correlated outage
  depends on it. It exists because a circuit breaker defends against an upstream
  being *down*, not against one being *flaky*, and a simulator with no notion of
  time cannot tell those apart — which would make every breaker measurement a
  measurement of the wrong thing.

## The catalogue

| Tier | Input $/M | Output $/M | Micro-cents per 1k in | Relative latency |
| --- | --- | --- | --- | --- |
| `nano` | $0.10 | $0.40 | 10,000 | 1x |
| `mini` | $0.60 | $2.40 | 60,000 | 6x |
| `flagship` | $3.00 | $12.00 | 300,000 | 30x |

The prices are in the shape real providers publish them — dollars per million
tokens — and are parsed from strings, never entered as floats.

## Money is an integer

Every amount in this project is an integer count of **micro-cents**: one
millionth of a cent, so one dollar is 100,000,000.

Floating-point money is the oldest bug in software, and this project would hit
it immediately. A gateway sums a cost over 2,400 requests and compares the total
against a budget threshold; a router picks between two policies whose costs
differ in the seventh decimal place. `0.1 + 0.2 != 0.3` decides which policy
wins, and the answer changes with summation order.

So:

* prices are **parsed from decimal strings** with at most `MAX_PRICE_DECIMALS`
  places, never constructed from a float literal;
* `cost_of(tokens, price_per_1k)` **rounds up**. A gateway that rounds a
  fractional micro-cent down on every request under-reports its own spend,
  systematically and in the direction that flatters it;
* totals are integer sums, so they are exact, associative, and identical on
  every platform.

`format_micro_cents` renders for humans (`$0.04596470`); nothing reads it back.

### Token counts are crude on purpose

`estimate_tokens` is four characters to a token. That is stated plainly rather
than hidden behind a real tokeniser, because a real tokeniser would make the
figure look authoritative while still being wrong for any provider with a
different vocabulary. Every cost in this project is a **relative** comparison
between policies over identical traffic, so a systematic bias in the token
estimate cancels out of every claim made here. An absolute dollar figure from
this gateway is not a forecast of anybody's bill.

## The real upstream

`amg.upstream.ollama` talks to a local Ollama server. No credentials, no bill,
no vendor account — which is why it is the one real integration in a portfolio
project that must be safe to publish.

It is **deliberately not on the experiment path**:

* it is not deterministic, and `amg.replay` exists to fail loudly when a replay
  does not reproduce;
* it produces wall-clock latency, and the resilience sweep runs on a virtual
  clock so that a queueing result is a queueing result rather than a
  measurement of one laptop;
* nobody reproducing this repository has the same models at the same
  quantisation, so a baseline built from real calls would fail for every reader.

Install it with `uv sync --extra ollama`, and see
[the simulator](simulator.md#how-far-the-table-sits-from-real-models) for what
it found.

## See also

* [The simulator, and its limits](simulator.md)
* [Routing and the estimator](routing.md)
