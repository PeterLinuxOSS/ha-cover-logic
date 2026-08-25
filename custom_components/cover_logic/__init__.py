"""Cover Logic: a universal rule-based cover (blind) controller.

Phase 1 is the decision core only (`config_schema`, `model`, `world`,
`conditions`, `engine`, `validation`) -- pure Python, no Home Assistant
imports, tested standalone. There is no `async_setup_entry` yet; wiring this
into a live config entry is a later phase.
"""
