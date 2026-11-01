"""PhantomDeps-CPS: measuring phantom dependencies in LLM-generated CPS
build manifests and gating them with three-state evidence.

Modules
-------
corpus      task corpus and prompt rendering (versioned, hashed)
llm         OpenAI-compatible client and resumable generation runner
extract     dependency-declaration extraction per ecosystem
registries  registry snapshots and resolvers with publication times
builtins    toolchain-provided names (Arduino cores, ESP-IDF, MicroPython)
gate        evidence model, integrity index, admission rule, baselines
stats       interval estimates
analysis    results.json, LaTeX macros, tables and figures

Every number written by :mod:`analysis` carries a provenance tag
(``measured``, ``computed``, ``assigned``); nothing is typed by hand.
"""

__version__ = "1.0.0"
