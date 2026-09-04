"""
core package
------------
Exposes the Revenue Recovery Agent's core decision-engine classes as a
single, stable import surface:

    from core import FailureClassifier, RetryEngine, StoppingRules

instead of reaching into each submodule individually. `__all__` is kept
explicit so this package's public API is obvious at a glance -- anything
not listed here should be treated as an internal implementation detail of
its submodule.
"""

from core.failure_classifier import FailureClassifier
from core.retry_engine import RetryEngine
from core.stopping_rules import StoppingRules

__all__ = [
    "FailureClassifier",
    "RetryEngine",
    "StoppingRules",
]
