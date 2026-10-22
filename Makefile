.PHONY: wellness-schedule langsmith-check up-all down-all seed-docker twilio-check demo dashboard up down install migrate seed run test eval eval-ollama eval-claude llm-check ollama-pull worker

up:
	docker compose up -d

down:
	docker compose down

# Everything in containers: database, Temporal, API, worker, dashboard (http://localhost:3000)
up-all:
	docker compose --profile app up -d --build

down-all:
	docker compose --profile app down

seed-docker:
	docker compose --profile app run --rm api python3 -m scripts.seed

install:
	cd backend && python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

migrate:
	cd backend && alembic upgrade head

seed:
	cd backend && python3 -m scripts.seed

run:
	cd backend && uvicorn app.main:app --reload

dashboard:
	cd frontend && npm install && npm run dev

test:
	cd backend && pytest

eval:
	cd backend && python3 -m app.evals.run --llm heuristic --label heuristic-baseline

eval-ollama:
	cd backend && python3 -m app.evals.run --llm ollama --label ollama-baseline

eval-claude:
	cd backend && python3 -m app.evals.run --llm anthropic --label claude-baseline

llm-check:
	cd backend && python3 -m app.agent.check

ollama-pull:
	ollama pull $${OLLAMA_MODEL:-llama3.2}

worker:
	cd backend && python3 -m app.workflows.worker

demo:
	cd backend && python3 -m app.demo

twilio-check:
	cd backend && python3 -m app.channels.smoke_test $(TO)

langsmith-check:
	cd backend && python3 -m app.agent.trace_check

wellness-schedule:
	cd backend && python3 -m app.booking.schedule
