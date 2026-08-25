"""The policy kernel — the deterministic authority.

Pure Python. No LLM. No network except the Razorpay client in
`kernel.payments`, which is the only module in the whole project that holds
payment credentials.

Nothing under `vyapaari/` may import from this package's `payments` module;
`tests/test_import_boundary.py` walks the AST of every vyapaari module and
fails the build if that edge exists.

Modules: payments.py (the Razorpay client), and the decision path itself —
bounds.py, gates.py, receipt.py.
"""
