"""Generate docs/RESEARCH.md from nexus/registry/catalog.tsv (single source of truth)."""
import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rows = list(csv.DictReader(open(ROOT / "nexus/registry/catalog.tsv", encoding="utf-8"), delimiter="\t"))
CLASSES = [("FOSS", "Fully free and open-source"), ("FREE-COMMERCIAL-RESTRICTED", "Free, but with commercial restrictions on the weights"),
           ("SOURCE-AVAILABLE", "Source-available (not open source)"), ("RESEARCH-ONLY", "Research-only / non-commercial"),
           ("HOSTED-FREE-TIER", "Free-tier hosted services"), ("EXCLUDED-PAID", "Paid options - excluded")]
AREAS = {"editing": "Editing & media engine", "video-gen": "AI video generation", "image-gen": "Image generation & workflows", "speech": "Speech recognition & subtitles",
         "tts": "Text-to-speech", "translation": "Translation", "llm": "Local language models", "animation": "Animation & motion graphics", "audio": "Audio production",
         "enhance": "Visual enhancement & vision", "orchestration": "Backend & orchestration", "media": "Free media & research sources", "compute": "Free compute",
         "models": "Model sources", "paid": "Paid services"}


def link(r):
    repo = r["repo"]
    if repo.startswith("http"):
        return f"[{r['name']}]({repo})"
    if repo.count("/") == 1 and " " not in repo:
        return f"[{r['name']}](https://github.com/{repo})"
    return r["name"]


d = Counter(r["decision"] for r in rows)
out = ["# Open-Source Discovery Report", "",
       f"{len(rows)} projects and services evaluated. Repository licence, last push, archive flag and stars were read from the live GitHub API on 2026-11-11; "
       "model-weight licences were read from the model cards. Entries without a GitHub repository were checked on their own sites.", "",
       f"**Decisions:** {d['DEPLOYED']} deployed and tested in this build, {d['NEXT']} selected for the next phase, {d['CANDIDATE']} candidates, {d['REJECTED']} rejected.", "",
       "- **DEPLOYED** = installed here, exercised by the acceptance tests.", "- **NEXT** = chosen, not yet integrated.",
       "- **CANDIDATE** = viable alternative, not chosen now.", "- **REJECTED** = licence, maintenance or fit problem (reason given).", "",
       "## Licence classes (kept separate)", ""]
for key, title in CLASSES:
    sub = [r for r in rows if r["class"] == key]
    out += [f"### {title} ({len(sub)})", "", ", ".join(f"{r['name']}" for r in sub), ""]
out += ["## Full matrix by area", ""]
for area, title in AREAS.items():
    sub = [r for r in rows if r["category"] == area]
    if not sub:
        continue
    out += [f"### {title}", "", "| Project | Licence (code) | Weights / asset licence | Last push | Stars | Decision | Why |", "|---|---|---|---|---|---|---|"]
    for r in sorted(sub, key=lambda r: ["DEPLOYED", "NEXT", "CANDIDATE", "REJECTED"].index(r["decision"])):
        arch = " **(archived)**" if r["archived"] == "true" else ""
        stars = f"{int(r['stars']):,}" if r["stars"].isdigit() and int(r["stars"]) else "-"
        out.append(f"| {link(r)}{arch} | {r['license']} | {r['weights_license'] or '-'} | {r['pushed']} | {stars} | **{r['decision']}** | {r['reason']} |")
    out.append("")
(ROOT / "docs/RESEARCH.md").write_text("\n".join(out), encoding="utf-8")
print(len(rows), dict(d))
