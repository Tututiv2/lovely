"""The desktop UI. Nothing in the core imports this package -- the dependency is one way.

That is what lets ``tests/run_tests.py`` exercise every hard part (merge order, rules,
argument templating, the auth chain) with no window, no display and no game.
"""
