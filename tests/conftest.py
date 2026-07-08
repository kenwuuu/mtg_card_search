# test_all_cards.py is the slow, exhaustive suite that needs a running server
# and real card data — it's run directly (python3 tests/test_all_cards.py, see
# README), not as part of the routine `pytest tests/` run.
collect_ignore = ["test_all_cards.py"]
