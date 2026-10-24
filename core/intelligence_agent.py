"""Moved to core/intelligence/agent.py.

Alias until every importer says `intelligence.agent`: the old name resolves
to the SAME module object, so imports and monkeypatches on either name hit
one module.
"""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("intelligence.agent")
