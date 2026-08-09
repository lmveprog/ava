.PHONY: install run doctor test clean

# first time here? this is the whole install.
install:
	python3 bootstrap.py

run:
	.venv/bin/ava

doctor:
	.venv/bin/ava-doctor

test:
	.venv/bin/python -m unittest discover -s tests -v

# removes the venv and every cache — config.json and your tokens stay.
clean:
	rm -rf .venv build dist src/*.egg-info .cache/ava_welcome
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
