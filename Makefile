.PHONY: install test run attack

install:
	pip install -r requirements.txt

test:
	pytest

run:
	python -m hisaab.cli

# Adversarial scoreboard for the demo video: N attacks attempted, M reached
# the output. M must be 0. Exit 1 if any attack leaks.
attack:
	python -m pytest -s -q --no-header tests/test_adversarial.py -k scoreboard
