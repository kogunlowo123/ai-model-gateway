"""Retries, backoff and circuit breaking -- and the load they create.

Kept apart from the routing policy because the experiments vary them
independently: the routing experiment holds resilience fixed and the resilience
experiment holds routing fixed. Two knobs in one object would let a change to
either quietly move the other's results.
"""
