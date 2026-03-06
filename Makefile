.PHONY: dashboard ingest-jsonl ingest-live

dashboard:
	@streamlit run dashboard/app.py

ingest-jsonl:
	@echo "Usage: make ingest-jsonl INPUT=path/to/log.jsonl DB=.dashboard/kalshi_edge_dashboard.sqlite"
	@test -n "$(INPUT)"
	@python3 -m dashboard.ingest.ingest_jsonl --input "$(INPUT)" --db "$(DB)"

ingest-live:
	@echo "Usage: make ingest-live INPUT=path/to/log.jsonl DB=.dashboard/kalshi_edge_dashboard.sqlite"
	@test -n "$(INPUT)"
	@python3 -m dashboard.ingest.ingest_live --input "$(INPUT)" --db "$(DB)"

