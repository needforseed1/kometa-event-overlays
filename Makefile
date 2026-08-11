.PHONY: test validate build

test:
	python3 -m unittest discover -s imdb-sync -p 'test_*.py'
	python3 -m unittest discover -s scoped-overlay -p 'test_*.py'

validate:
	docker compose config --quiet

build:
	docker compose build
