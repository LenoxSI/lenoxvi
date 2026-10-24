"""Moved to core/intelligence/stub.py.

Alias until every importer says `intelligence.stub`: the old name resolves
to the SAME module object, so imports and monkeypatches on either name hit
one module.
"""
import importlib
import sys

sys.modules[__name__] = importlib.import_module("intelligence.stub")
