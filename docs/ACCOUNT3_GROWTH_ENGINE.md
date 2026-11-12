# Account 3 growth engine (agent F): experiments, statistics, learning loop, recommendations

Status: research notes (code follows in `nexus/intelligence/growth/`).

## Research notes (sources)

- YouTube "Test & compare" thumbnails: up to three thumbnails; each gets a result based on **watch time share**;
  outcomes include "Winner", "Preferred" (likely best, gap not large enough to declare a winner) and
  "Inconclusive". https://support.google.com/youtube/answer/13861714
- YouTube "A/B test titles & thumbnails": up to 3 titles / title+thumbnail combinations, the one with the highest
  watch time is shown to all viewers at the end. https://support.google.com/youtube/answer/16391400
  (Read via search summary on 2026-11-12; exact current UI wording unverified.)
  Consequence for NEXUS: such tests are a *platform feature*; NEXUS only records the result the owner enters
  (`experiments.record_platform_result`), it never fakes the split or re-computes YouTube's verdict.
- Public dataset used for the demo: "YouTube Video Metadata", Tisha Nawani, Mendeley Data V1 (2026-05-12),
  DOI 10.17632/4gf2nhbr9d.1, licence **CC BY 4.0** (read from the Mendeley public API `data_licence` field),
  https://data.mendeley.com/datasets/4gf2nhbr9d/1 . 3,951 videos collected with the YouTube Data API v3 across
  search queries (music, gaming, sports, education, ...). File `data.csv` 5.7 MB,
  sha256 56c938bc9c69a368929c320f54e3d32ec06b916945c0b74abdb57719929abc8e. Stored in `.nexus/a3_data/` (not in Git).
  It is **not the owner's channel**; it is a search-sampled cross-section with one snapshot per video and an
  unknown collection date, so age normalisation is approximate (see Limitations).
- Kaggle "Trending YouTube Video Statistics" (datasnaek) was considered; its CC0 status was only seen in a
  secondary source and download needs a Kaggle login, so it was not used.
</content>
</invoke>
<invoke name="Bash">
<parameter name="command">cd /home/user/new-project-money/.claude/worktrees/agent-ab9ae4c5c7c3067bf && git add docs/ACCOUNT3_GROWTH_ENGINE.md && git commit -qm "Account 3 growth (F): research notes - YouTube Test & compare, licensed public dataset

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01SbTugRkmz2ogJfvrVUGQbr" && (git push -u origin work/account-3-f-growth 2>&1 | tail -2)