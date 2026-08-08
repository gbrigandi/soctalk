"""What models cost, and how a run learns it (#125).

Pricing used to be a hardcoded table plus an env overlay, keyed by model string
alone. That is wrong twice over: nobody maintains a table in code, and a model
string does not identify a price — the same model ID measured 2.3x apart on two
gateways in our own benchmark, because price is a property of the provider and
the model together.

This package holds the replacement:

- ``catalog`` — the install-level price catalog, a global table.
- ``resolve`` — turning a tenant's LLM config into the rates a run is priced
  at, and stamping them on the run so the answer never changes underneath it.

What it deliberately does not do is decide what happens when a price is
unknown; that is #124's question, and this package's job is only to make
"unknown" rarer and always say so out loud.
"""
