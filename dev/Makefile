.PHONY: dev-up dev-down dev-logs dev-mock dev-reset

# Boot HA + MockPanel stack
dev-up:
	docker compose -f docker-compose.yml up -d
	@echo
	@echo "HA  →  http://localhost:8123  (first-run onboarding required)"
	@echo "Mock panel →  host.docker.internal:14369"
	@echo "Controller key →  000102030405060708090a0b0c0d0e0f"

dev-down:
	docker compose -f docker-compose.yml down

dev-logs:
	docker compose -f docker-compose.yml logs -f

# Run only the mock on the host (no docker), useful when you want to
# point a host-side OmniClient at it.
dev-mock:
	cd .. && uv run python dev/run_mock_panel.py --host 127.0.0.1 --port 14369

# Wipe the HA config dir (clears onboarding + entities).
dev-reset:
	docker compose -f docker-compose.yml down
	rm -rf ha-config
	mkdir -p ha-config
