"""Localisation intelligence on top of nexus.localize (agent D, Account 3).

  matrix        language matrix (ASR / MT / TTS voices with licences / subtitle rules) - matrix.yaml
  translation_qa  omission/addition/entity/glossary/repetition/unsupported-content QA + LocalizationManifest
  dialect       Egyptian vs Gulf Arabic profiles, classifier, adaptation (router + lexical fallback)
  dubbing       dubbing manifest, licence-gated voice selection, consent records for cloning
  subtitles     SRT / VTT / ASS exporters, full vs condensed caption products

Nothing here renders audio or video (Account 2) or edits nexus/localize.py (integration requests are in
docs/ACCOUNT3_LANGUAGE_MATRIX.md).
"""
from __future__ import annotations

