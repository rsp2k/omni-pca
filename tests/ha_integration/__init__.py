"""HA-side integration tests.

These spin up a real Home Assistant instance in-process via
``pytest-homeassistant-custom-component``, point the omni_pca config
entry at a live ``MockPanel`` running on a localhost port, and assert
that the entity layer materializes correctly.
"""
