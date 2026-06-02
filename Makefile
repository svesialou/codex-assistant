.PHONY: test docker-config docker-build docker-up docker-down docker-logs install reindex

test:
	python -m unittest discover -s tests

docker-config:
	docker compose config

docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs --tail=120 codex-telegram-bot

install:
	./scripts/install.sh

reindex:
	python -m codex_telegram_bot --reindex
