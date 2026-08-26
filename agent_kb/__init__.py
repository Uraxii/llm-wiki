"""agent-kb: an LLM wiki over an environment.

This package is the INGEST half. `collect` runs connector plugins on a
schedule and `write.ingest` is the one mutator that reaches the store.
The read tools live elsewhere and are not part of this package.
"""
