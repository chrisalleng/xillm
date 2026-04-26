"""agent_core — orchestrator for the FFXI autonomous agent.

Modules:
    main             entrypoint; run-loop wiring nav + future subsystems together
    nav              navmesh build + path requests (the original navserver)
    config           runtime configuration (character name, paths, LLM models)
    state            state aggregator (per-character state file polling)
    events           append-only cross-addon event log
    persistence      load/save persistent JSON files (goals, gambits, knowledge)
    llm_gateway      Anthropic API client + tool surface (stub at Phase 1)
    dropoff_detect   offline drop-off candidate detector (CLI)

The architecture is documented in docs/agent-architecture.md."""
