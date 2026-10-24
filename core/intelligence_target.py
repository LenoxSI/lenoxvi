"""Moved to core/intelligence/target.py.

Alias until every importer says `intelligence.target`: the old name resolves
to the SAME module object, so imports and monkeypatches on either name hit
one module.
"""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("intelligence.target")
