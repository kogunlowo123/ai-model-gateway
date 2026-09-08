"""The HTTP surface: an OpenAI-compatible subset, and the operational endpoints.

Thin by design. Everything it does is delegated to :mod:`amg.gateway`, because a
gateway whose served path differs from its measured path publishes numbers about
software nobody is running.
"""
