"""Money, in integers.

Every cost in this project is an :class:`int` count of **micro-cents** --
millionths of a US cent, so one dollar is 100_000_000. Nothing here is ever a
float, and that is a decision rather than a style preference.

Floating-point dollars do not add up. Summing a per-request cost over a month of
traffic accumulates representation error that grows with the number of terms,
and the direction of the drift depends on the order the terms arrived in. A
gateway whose invoice disagrees with the sum of its own request log by a few
cents is a gateway nobody can reconcile, and the bug is invisible in every test
that adds fewer than a few thousand numbers.
``tests/unit/test_money.py`` measures the drift rather than asserting it.

Micro-cents rather than cents because per-token prices are small: a model at
$0.15 per million input tokens costs 15 micro-cents per thousand tokens, which
is an exact integer here and 1.5e-7 dollars in a float.

Rounding is **up**, always, at the point a price meets a token count. A gateway
that rounds to nearest can charge less than it was charged, and the shortfall is
systematic rather than random because it applies to every small request.
"""

from __future__ import annotations

from typing import Final

#: Micro-cents in one US cent.
MICRO_CENTS_PER_CENT: Final[int] = 1_000_000

#: Decimal places a quoted price may carry. Eight is exactly the point at
#: which a dollars-per-million price becomes finer than one micro-cent per
#: thousand tokens, so anything beyond it would round silently.
MAX_PRICE_DECIMALS: Final[int] = 8

#: Micro-cents in one US dollar.
MICRO_CENTS_PER_DOLLAR: Final[int] = 100 * MICRO_CENTS_PER_CENT

#: Prices are quoted per this many tokens. Providers quote per million; per
#: thousand keeps the integers small enough to read in a report without losing
#: anything, since a per-million price is always a multiple of a per-thousand
#: one at this resolution.
TOKENS_PER_PRICE_UNIT: Final[int] = 1_000


def dollars_per_million_to_price(dollars: str) -> int:
    """Convert a provider's quoted price into micro-cents per 1k tokens.

    The argument is a **string**, not a float, because the whole point of this
    module is that a price never exists as a float even briefly. ``"0.15"``
    means fifteen cents per million tokens.

    Raises:
        ValueError: if the string is not a plain decimal, or carries more than
            eight decimal places -- beyond which the price is finer than a
            micro-cent per thousand tokens and would silently round.
    """
    text = dollars.strip()
    if not text or text.startswith(("+", "-")):
        raise ValueError(f"a price must be a plain non-negative decimal, got {dollars!r}")
    whole, _, fraction = text.partition(".")
    if not whole.isdigit() or (fraction and not fraction.isdigit()):
        raise ValueError(f"a price must be a plain non-negative decimal, got {dollars!r}")
    if len(fraction) > MAX_PRICE_DECIMALS:
        raise ValueError(
            f"price {dollars!r} has more precision than a micro-cent per 1k tokens can carry"
        )
    scaled = int(whole + fraction.ljust(MAX_PRICE_DECIMALS, "0"))
    # dollars per 1e6 tokens -> micro-cents per 1e3 tokens is a factor of
    # 100 (cents) * 1e6 (micro) / 1000 (per-thousand), all exact in integers.
    price: int = scaled * MICRO_CENTS_PER_DOLLAR // 10**MAX_PRICE_DECIMALS // TOKENS_PER_PRICE_UNIT
    return price


def cost_of(tokens: int, price_per_1k: int) -> int:
    """Cost in micro-cents of *tokens* at *price_per_1k*, rounded **up**.

    Rounding up rather than to nearest: a gateway that rounds to nearest bills
    less than it was billed on roughly half of all requests, and because small
    requests round down proportionally more often the shortfall is systematic.
    Over-recovering by less than a micro-cent per request is the safe direction.
    """
    if tokens < 0:
        raise ValueError("a token count cannot be negative")
    if price_per_1k < 0:
        raise ValueError("a price cannot be negative")
    return -(-tokens * price_per_1k // TOKENS_PER_PRICE_UNIT)


def format_micro_cents(amount: int) -> str:
    """Render micro-cents as dollars, exactly, for a human.

    Built by slicing the integer rather than by dividing, so nothing in this
    module ever produces a float -- including the string a report prints.
    """
    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)
    whole, remainder = divmod(magnitude, MICRO_CENTS_PER_DOLLAR)
    return f"{sign}${whole}.{remainder:08d}"
