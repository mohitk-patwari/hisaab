.PHONY: install test run

install:
	pip install -r requirements.txt

test:
	pytest

run:
	python -m hisaab.cli
