.PHONY: test validate pull build

test:
	python3 -m unittest discover -s imdb-sync -p 'test_*.py'
	python3 -m unittest discover -s scoped-overlay -p 'test_*.py'

validate:
	docker compose config --quiet

pull:
	docker compose pull

build:
	docker compose -f compose.yml -f compose.build.yml build
