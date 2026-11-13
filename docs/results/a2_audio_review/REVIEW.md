# Audio listening sheet (Account 2)

Status: **PENDING OWNER REVIEW**. Nobody has listened to these files yet.

The audio is not in Git (no media in the repository). Generate it on your PC (about 1 minute after the benchmark
media is fetched): `python scripts/benchmarks/fetch_media.py` then `python scripts/acceptance/audio_review_package.py`
(add `--master <Demo 1 master.mp4>` for the programme-mix sample). Files appear in `%NEXUS_HOME%/review/a2_audio/`.

| Item | Files | Chain applied | Before → after (LUFS / SNR dB) | Check for | Verdict |
|---|---|---|---|---|---|
| interview (interview) | interview_before.ogg / interview_after.ogg | dc_highpass, denoise, eq, compress, loudness | -27.05 → -16.05 / 27.45 → 31.96 | no automated warning; listen anyway | ☐ approve ☐ notes ☐ reject |
| panel (studio) | panel_before.ogg / panel_after.ogg | dc_highpass, dehum, eq, speakers, compress, roomtone, loudness | -21.97 → -16.01 / 43.4 → 41.2 | no automated warning; listen anyway | ☐ approve ☐ notes ☐ reject |
| podcast (interview) | podcast_before.ogg / podcast_after.ogg | dc_highpass, dehum, denoise, eq, compress, loudness | -21.36 → -16.03 / 34.25 → 32.4 | no automated warning; listen anyway | ☐ approve ☐ notes ☐ reject |
| walktalk (outdoor) | walktalk_before.ogg / walktalk_after.ogg | dc_highpass, denoise, eq, compress, loudness | -32.4 → -16.05 / 21.51 → 28.15 | no automated warning; listen anyway | ☐ approve ☐ notes ☐ reject |
| ad (archival) | ad_before.ogg / ad_after.ogg | dc_highpass, denoise, eq, compress, loudness | -18.77 → -16.0 / 18.95 → 22.51 | no automated warning; listen anyway | ☐ approve ☐ notes ☐ reject |
| documentary (outdoor) | documentary_before.ogg / documentary_after.ogg | dc_highpass, denoise, eq, compress, loudness | -14.61 → -16.0 / 21.63 → 23.4 | no automated warning; listen anyway | ☐ approve ☐ notes ☐ reject |
| programme mix | programme_mix.ogg | Demo 1 mix | whole master -14.17 LUFS | music level under narration (should never mask words); music recovery in pauses (no pumping); level steps at cuts; sync of words to picture | ☐ approve ☐ notes ☐ reject |

Reviewer: ____________  Date: ________  Headphones / speaker used: ____________
