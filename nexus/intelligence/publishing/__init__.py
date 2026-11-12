"""Account 3 / agent E - platform intelligence, publishing and analytics import.

    capabilities   matrix() / get(p) / limits(p) / registry_diff()   media specs (platforms.yaml) + API reality (capabilities.yaml)
    engine         connect_account / create / approve / enqueue / process / run_one / retry_failed / history
    packaging      package(platform, transcript, ...) -> PlatformMetadata (router "title_generation", deterministic fallback)
    analytics      import_csv(path, kind) / youtube_daily / youtube_retention -> AnalyticsRecord (stored, never invented)
    adapters       youtube (official API, private by default), tiktok / instagram / facebook (gated), snapchat (manual)
    youtube_oauth  owner's own OAuth client, loopback + PKCE; tokens via Account 1 vault (creds.py) or env
    jobs_handler   durable `publish-upload` job kind (wire it in nexus/workflows.py)

Docs: docs/ACCOUNT3_PLATFORM_CAPABILITIES.md. Nothing here uploads publicly without approval == 'approved_by_owner'.
"""
