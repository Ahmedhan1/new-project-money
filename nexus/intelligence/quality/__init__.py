"""Account 3 quality gates that run BEFORE anything is rendered or published: typography (glyph coverage + OCR
read-back), visual context (geography/period/asset kind), scene visual plans, factual hooks. Each gate returns
{ok, findings[], evidence} and never silently substitutes a fallback."""
