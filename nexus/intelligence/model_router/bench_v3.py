"""Model benchmark V3: the V2 items plus new ones so that EVERY routed capability has at least 15 items, all scored by
deterministic rules (no model judges another). Same harness as V2 (Ollama on an authorised Kaggle GPU, temperature 0,
identical prompts for every model); only the item set and the scorers below are new.

    python -m nexus.intelligence.model_router.bench_v3 kernel --models a,b --out DIR --slug nexus-a3-bench-v3-a
    python scripts/account3/bench_v3_score.py OUT.json results_a.json [...]

Capabilities (>= 15 items each): reasoning, long_context, grounding, conflicts, json, summarization, translation,
dialect_egy, dialect_gulf, writing, strategy, instruction. `dialect` = mean of the two dialect capabilities.

Scoring rules (all 0..1 per item, capability = mean, with a 95 % normal-approximation interval):
  answer       JSON {"answer"}; gold (or any alternative) must appear as a whole word / whole number (1,000 = 1000;
               number words one..twenty = digits). `unanswerable`: must say not stated/unknown and contain no digit.
  conflict     JSON {"agree", "values"}: 0.5 for the right agree flag + 0.5 for listing the conflicting values (or the
               agreed value without a second one).
  extract      share of gold fields matched (numbers equal, strings contained after normalisation, lists as sets).
  checks       JSON with declared checks (equality, length, set, range, words per element).
  summary      0.2 within the word limit + 0.6 share of key-idea groups present + 0.2 no number absent from the source;
               0 when no key idea is present.
  translation  0.6 x min(1, chrF(output, reference) / 0.6) + 0.25 x target-script share + 0.15 x source numbers kept;
               0 when the output is closer to the English source than to the reference (untranslated copy) or
               chrF < 0.15 (unrelated text).
  dialect      0.2 Arabic script + 0.4 own-dialect markers (2 distinct = full) + 0.2 no other-dialect marker + 0.2 at
               most one Modern-Standard-Arabic marker; the last two count only when own markers are present. Lexicon
               markers are a proxy - a native reviewer remains mandatory for any published dialect text.
  grounded     one sentence: word limit, no sensational wording, numbers only from the facts sources agree on, none of
               the disputed or single-source numbers; 0 when the subject is not mentioned.
  titles/hook  count, length, keyword, uniqueness, no clickbait, no invented numbers (hook: 0 without the keyword).
  strategy     V2 rule set with the item's own brief; 0 without a hook and beats (applies to the V2 item too).
  constraints  share of declared formatting constraints satisfied; 0 when none of the positive constraints (what to
               produce) holds, so an empty or one-word reply cannot score on "avoid X" constraints alone.
V2 items keep their V2 scorers, except translation/dialect, which use the V3 rules above (reference-based, so no
back-translation model is needed and the score is reproducible anywhere), and `grounded_hook`, scored by the V3
grounded rule (the V2 rule did not check that the sentence is about the footbridge)."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import bench as b1
from . import bench_v2 as b2

NUM_WORDS = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                                             "fifteen sixteen seventeen eighteen nineteen twenty".split())}
_EAST = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹०१२३४५६७८९", "0123456789" * 3)

# Dialect markers: words typical of one dialect and unusual in Modern Standard Arabic and in the other dialect.
EGY = sorted(set(b1.EGY) | {"عاوز", "عاوزة", "كدا", "بتاعة", "دول", "علشان", "النهارده", "النهاردة", "امبارح", "إمبارح", "فين", "ليه",
                            "بقى", "خالص", "حاجة", "زي", "برضه", "لسه", "هنسافر", "هشتري", "هاشتري", "مستنيك", "استناني", "عيش",
                            "أجزخانة", "اجزخانة", "ازيك", "إزيك", "شفتك", "شوفتك", "ماكلمتنيش"})
GULF = sorted(set(b1.GULF) | {"هالحين", "يبي", "تبي", "نبي", "يبغى", "تبغى", "نبغى", "ابغي", "وش", "ليش", "مب", "مو", "ترى", "كذي",
                              "چذي", "هاذا", "بنسافر", "بشتري", "ياي", "شخبارك", "شحالك", "باچر", "دقايق"})
MSA = ["سوف", "لماذا", "ماذا", "الآن", "أريد", "ليس", "كيف", "لدينا", "لديك", "إننا", "لكي", "يوجد", "سنسافر", "انتظرني"]
S_CONFLICT = {"type": "object", "required": ["agree", "values"], "properties": {"agree": {"type": "boolean"},
                                                                                 "values": {"type": "array", "items": {"type": "string"}}}}


def _norm_num(s: str) -> str:
    s = (s or "").translate(_EAST)
    s = re.sub(r"(?<=\d)[,٬   ](?=\d{3}\b)", "", s)
    return re.sub(r"\b(" + "|".join(NUM_WORDS) + r")\b", lambda m: NUM_WORDS[m.group(1).lower()], s, flags=re.I)


def _numbers(text: str) -> set[str]:
    return {n.rstrip(".") for n in re.findall(r"\d+(?:\.\d+)?", _norm_num(text))}


def _has(text: str, gold: str) -> bool:
    t, g = _norm_num(text).lower(), _norm_num(gold).lower().strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", g):
        return bool(re.search(rf"(?<![\d.]){re.escape(g)}(?!\.?\d)", t))
    return bool(re.search(rf"(?<!\w){re.escape(g)}(?!\w)", t))


def _alts(g) -> list[str]:
    return [g] if isinstance(g, str) else list(g)


def _json(text: str):
    return b1.parse_json(text)


# ================================================================= item data
REASON3 = [("If today is Wednesday, what day of the week is it 10 days from today?", "saturday"),
           ("A shirt costs 40 euros and is discounted by 25 percent. What is the new price in euros? Answer with the number.", "30"),
           ("A bag holds 3 red, 5 blue and 2 green balls. How many balls must you take out without looking to be sure of having two "
            "of the same colour? Answer with the number.", "4"),
           ("Tom has twice as many books as Sara. Together they have 36 books. How many books does Tom have?", "24"),
           ("A clock shows 09:45. What time does it show 2 hours and 30 minutes later? Answer as HH:MM.", "12:15"),
           ("All roses in the garden are red. Some red flowers in the garden are tulips. Is it certain that some roses are tulips? "
            "Answer yes or no.", "no"),
           ("A tank holds 1200 litres and fills at 40 litres per minute. How many minutes does it take to fill it from empty?", "30")]


def _doc(seed: int, filler, needles: dict[int, str], n: int = 28) -> str:
    paras = [filler(i) for i in range(n)]
    for k, v in needles.items():
        paras[k] += " " + v
    return "\n\n".join(paras)


NAMES = ["Arno Pell", "Bea Lund", "Cyril Hart", "Dora Quist", "Emil Sato", "Fenna Roos", "Gus Okafor", "Hedda Lim", "Ivo Brandt", "Juno Wilde"]


def long_documents() -> list[tuple[str, list[tuple[str, object]]]]:
    a = _doc(1, lambda i: (f"Section {i + 1}. Hall {i % 6 + 1} of Kestrel Works produced {300 + i * 17} lamps during week {i + 5}. Supervisor "
                            f"{NAMES[i % 10]} recorded that the varnish supply arrived {i % 5 + 1} days late, and the night shift repainted the "
                            f"loading dock. The model K{i % 5 + 1} lamp sold for {40 + i * 3} euros at the spring fair, and a visiting inspector "
                            f"counted {60 + i * 4} pallets in the yard. Orders from the northern dealers rose again, which kept the paint line "
                            "busy until the late autumn holidays and delayed the planned move of the archive to the old foundry."),
             {2: "Kestrel Works was founded in 1962 by the engineer Odile Varga.",
              9: "In 1988 Odile Varga handed the company to her nephew, Piet Varga, who led it until 2004.",
              12: "The model K7 lamp was launched at a price of 89 euros.",
              17: "From 2004 the managing director was Hana Ilic.",
              20: "The Brest warehouse closed in 2015; the Lodz warehouse is still in use.",
              25: "In 2019 the price of the K7 was raised to 99 euros; a 2023 notice lowered it again to 94 euros, the price that applies today."})
    qa = [("Who founded Kestrel Works?", "odile varga"), ("Who led Kestrel Works in 1995?", "piet varga"),
          ("According to the latest notice, what is the price of the K7 lamp today, in euros?", "94"),
          ("Which warehouse closed in 2015?", "brest")]
    b = _doc(2, lambda i: (f"Log entry {i + 1}. The research vessel Meridian Tern sampled water at station {i + 3}, logging {8 + i % 9}.{i % 10} "
                            f"degrees and a sounding of {900 + i * 71} metres. The cook baked bread for the {14 + i % 3} people on board, "
                            f"and the deck crew mended a net torn on the previous evening. Wind from the {['north', 'west', 'south-west'][i % 3]} "
                            f"made the afternoon transfer to the sampling boat slow, so only {3 + i % 4} of the planned casts were completed "
                            "before dark, and the remaining bottles were stored in the cold room for the next morning."),
             {5: "On day 6 the vessel reached Port Alda, where the botanist Mira Holt joined the crew.",
              13: "On day 14 an engine fault forced a two-day stop at Skerry Point.",
              19: "The deepest sounding of the voyage, 3,870 metres, was taken at station 41.",
              23: "On day 22 Mira Holt left the expedition at Cape Fennel to return home."})
    qb = [("At which place did the botanist join the crew?", "port alda"), ("How many days did the stop caused by the engine fault last?", "2"),
          ("Where did Mira Holt leave the expedition?", "cape fennel"), ("What was the deepest sounding of the voyage, in metres?", "3870")]
    c = _doc(3, lambda i: (f"Rule {i + 1}. On the Isle of Brannoch, vessels shorter than {8 + i} metres may moor at pier {i % 9 + 1} between "
                            f"{5 + i % 4}:00 and {18 + i % 5}:00. The fee at pier {i % 9 + 1} is {6 + i % 7} crowns per night, payable to the "
                            f"duty clerk {NAMES[(i + 3) % 10]}. Anchoring in the channel is forbidden during the {['spring', 'summer', 'autumn'][i % 3]} "
                            "regatta, and owners must log fuel deliveries in the harbour book before noon on the day of delivery."),
             {4: "Harbour master since 2021: Joran Teague. Before him, Elin Moss held the post for eleven years.",
              10: "Fees at pier A were set at 14 crowns per night in 2020 and increased to 18 crowns per night in 2024.",
              18: "Pier D is reserved for the ferry on Saturdays.",
              24: "Rule amendment (2024): fishing boats may moor at pier C free of charge."})
    qc = [("Who was harbour master before Joran Teague?", "elin moss"), ("What is the fee at pier A per night now, in crowns?", "18"),
          ("On which day of the week is pier D reserved for the ferry?", "saturday"),
          ("How much do fishing boats pay to moor at pier C?", ["free", "0", "nothing", "no charge"])]
    return [(a, qa), (b, qb), (c, qc)]


SRC_G = {
    "observatory": ("[S1] University press office (2022): The Lake Varno observatory opened in 2021. Its main telescope has a 2.4 m mirror.\n"
                    "[S2] Regional newspaper (2021): The observatory on Lake Varno, opened this year, houses a telescope with a 2.4 m mirror "
                    "and cost 31 million euros.\n"
                    "[S3] Travel forum post (2024): The observatory's telescope mirror is 3 m wide, the largest in the country."),
    "windfarm": ("[S1] Energy agency report (2023): The Harrow Valley wind farm has 24 turbines with a combined capacity of 96 MW.\n"
                 "[S2] Operator website (2023): Harrow Valley's 24 turbines can produce up to 96 MW.\n"
                 "[S3] Local radio (2022): Residents say the 30 turbines are noisy at night."),
    "tunnel": ("[S1] Transport ministry (2020): The Corrin rail tunnel is 11.2 km long and opened in December 2020.\n"
               "[S2] Engineering review (2021): Opened in December 2020, the 11.2 km Corrin tunnel took six years to build.\n"
               "[S3] Blog (2022): The 12 km Corrin tunnel opened in 2019."),
}
GROUNDED_QA = [("observatory", "In which year did the observatory open?", "2021"),
               ("observatory", "How much did the observatory cost, according to the sources?", "31"),
               ("windfarm", "What is the combined capacity of the wind farm in MW?", "96"),
               ("windfarm", "Which source gives a different number of turbines? Answer with its id.", "s3"),
               ("tunnel", "How many years did the tunnel take to build?", "6"),
               ("tunnel", "How long is the tunnel in km, according to the official sources?", "11.2")]
UNANSWERABLE = [("observatory", "How many people work at the observatory?"), ("windfarm", "In which year did construction start?"),
                ("tunnel", "How much did the tunnel cost?")]
GROUNDED_WRITE = [("observatory", "observatory", {"2021", "2.4"}, {"3", "31", "2022", "2024"}),
                  ("windfarm", "turbines", {"24", "96"}, {"30", "2022"}),
                  ("tunnel", "tunnel", {"11.2", "2020"}, {"12", "2019", "6"})]

CONFLICTS = [  # (sources, question, agree, [alternatives per expected value])
    ("[S1] The Belmar tower is 120 m tall.\n[S2] Belmar tower, 120 metres high, overlooks the bay.", "the height of the tower", True, [["120"]]),
    ("[S1] The Ostra bridge opened in 1998.\n[S2] The Ostra bridge opened to traffic in 2001.", "the opening year of the bridge", False, [["1998"], ["2001"]]),
    ("[S1] Census 2020: Arlo has 8,400 residents.\n[S2] Arlo, population about 8,400, lies on the coast.\n[S3] Arlo's 9,100 residents voted on Sunday.",
     "the population of Arlo", False, [["8400"], ["9100"]]),
    ("[S1] The Rovers club was founded in 1921.\n[S2] Founded in 1921, the Rovers club now has 600 members.", "the founding year of the club", True, [["1921"]]),
    ("[S1] The Tamsin river is 340 km long.\n[S2] The 340 km Tamsin river.\n[S3] Along its 340 km, the Tamsin river crosses two regions.",
     "the length of the river", True, [["340"]]),
    ("[S1] The Lindow festival takes place every June.\n[S2] The Lindow festival is held each July.", "the month of the festival", False, [["june"], ["july"]]),
    ("[S1] The Kessel stadium holds 42,000 spectators.\n[S2] The Kessel stadium has room for 45,000 fans.", "the capacity of the stadium", False,
     [["42000"], ["45000"]]),
    ("[S1] The novel 'Salt Roads' was written by Ana Pell.\n[S2] Ana Pell's novel 'Salt Roads' won a prize.", "the author of the novel", True,
     [["ana pell"]]),
    ("[S1] The Strela train reaches a top speed of 210 km/h.\n[S2] With a top speed of 230 km/h, the Strela train is the fastest in the region.",
     "the top speed of the train", False, [["210"], ["230"]]),
    ("[S1] Lake Vire is 18 m deep at its deepest point.\n[S2] Lake Vire has a maximum depth of 18 metres.\n[S3] Depth of Lake Vire: 18 m.",
     "the maximum depth of the lake", True, [["18"]]),
    ("[S1] The Hollis museum has 3 floors.\n[S2] The Hollis museum's four floors hold 2,000 objects.", "the number of floors", False, [["3"], ["4"]]),
    ("[S1] The ship Albatross was launched in 1912.\n[S2] Albatross, launched in 1912, sank in 1930.\n[S3] After its 1911 launch, the Albatross sailed to Oslo.",
     "the launch year of the ship", False, [["1911"], ["1912"]]),
    ("[S1] The film 'North Light' runs 104 minutes.\n[S2] 'North Light' is a 104-minute drama.", "the running time of the film", True, [["104"]]),
    ("[S1] Mount Seraph is 2,962 m high.\n[S2] Mount Seraph rises to 2,926 m.", "the height of the mountain", False, [["2962"], ["2926"]]),
    ("[S1] The Oberau running track is 5 km long.\n[S2] The 5,000 m Oberau track circles the lake.", "the length of the track", True, [["5", "5000"]]),
]

EXTRACT = [
    ("Invoice 2291 was issued on 3 February 2026 by Nordlys Print to Cafe Mira for 640 euros, payable within 30 days.",
     {"invoice": "2291", "date": "2026-02-03", "seller": "Nordlys Print", "buyer": "Cafe Mira", "amount_eur": 640, "due_days": 30}),
    ("Flight LX 318 from Zurich to Lisbon departs at 06:55 and arrives at 08:50 local time.",
     {"flight": "LX 318", "from": "Zurich", "to": "Lisbon", "departs": "06:55", "arrives": "08:50"}),
    ("The Harbour Jazz Night takes place on 12 July 2026 at the Old Customs House in Gdansk. Tickets cost 35 zloty; doors open at 19:00.",
     {"event": "Harbour Jazz Night", "date": "2026-07-12", "venue": "Old Customs House", "city": "Gdansk", "price": 35, "doors": "19:00"}),
    ("On 9 May 2025 the members elected Dr. Samira Nassar as chair, Leo Brandt as treasurer and Yuki Tan as secretary.",
     {"chair": "Samira Nassar", "treasurer": "Leo Brandt", "secretary": "Yuki Tan", "date": "2025-05-09"}),
    ("The Vela 2 backpack weighs 1.2 kg, holds 28 litres and costs 129 dollars; it comes in grey and olive.",
     {"product": "Vela 2", "weight_kg": 1.2, "volume_l": 28, "price_usd": 129, "colours": ["grey", "olive"]}),
    ("وصل القطار رقم 905 إلى الإسكندرية الساعة 14:20 يوم 5 مارس 2026.",
     {"train": "905", "city": "الإسكندرية", "time": "14:20", "date": "2026-03-05"}),
    ("Mix 300 g of rice with 600 ml of water, bring it to the boil and cook it covered for 18 minutes.",
     {"rice_g": 300, "water_ml": 600, "minutes": 18}),
]
CHECKS = [
    ('Return JSON describing a 45-second French video about recycling glass with keys "title" (string), "language" (ISO 639-1 code), '
     '"duration_s" (number) and "tags" (exactly 4 strings).', ["title", "language", "duration_s", "tags"],
     {"eq": {"language": "fr", "duration_s": 45}, "len": {"tags": 4}, "nonempty": ["title"]}),
    ('Return JSON {"steps": [...]} with exactly 5 short steps to plant a tree, each step a string of at most 10 words.', ["steps"],
     {"len": {"steps": 5}, "max_words_each": {"steps": 10}}),
    ('Return JSON with keys "city" (string), "country_code" (ISO 3166-1 alpha-2) and "population_millions" (number) for this fact: '
     'Cairo, the capital of Egypt, has about 10 million people in the city proper.', ["city", "country_code", "population_millions"],
     {"eq": {"country_code": "EG", "city": "Cairo"}, "range": {"population_millions": [9, 11]}}),
    ('Classify the viewer comment "The audio was too quiet but the story was great" as JSON with "sentiment" (one of "positive", '
     '"negative", "mixed") and "topics" (a list chosen only from "audio", "story", "visuals", "length").', ["sentiment", "topics"],
     {"eq": {"sentiment": "mixed"}, "set": {"topics": ["audio", "story"]}}),
    ('Return JSON {"is_question": <boolean>, "language": <ISO 639-1 code>} for the text: "¿Dónde está la estación?"', ["is_question", "language"],
     {"eq": {"is_question": True, "language": "es"}}),
    ('Convert 135 minutes to JSON {"hours": <integer>, "minutes": <integer>} (minutes below 60).', ["hours", "minutes"],
     {"eq": {"hours": 2, "minutes": 15}}),
]

SUMMARIES = [  # (text, max_words, [key-idea groups])
    ("Mangrove forests grow along tropical coasts where salt water meets fresh water. Their tangled roots slow down waves and reduce "
     "erosion during storms. The roots also shelter young fish, crabs and shrimp, which makes mangroves important nurseries for fisheries. "
     "In addition, mangrove soils store large amounts of carbon, often more per hectare than inland forests. Many mangroves have been "
     "cleared for shrimp farms and coastal building, and replanting projects now try to restore them.",
     40, [("coast", "coastal"), ("wave", "erosion", "storm"), ("fish", "nurser"), ("carbon",), ("clear", "lost", "destroy", "replant", "restor")]),
    ("The town council approved a plan to close the main square to cars on weekends from April. Buses will be rerouted to two side "
     "streets, and delivery vans may enter only before 10 a.m. Shop owners were divided: some expect more pedestrians, others fear "
     "losing customers who arrive by car. The council will review the trial after six months.",
     35, [("square",), ("car", "traffic"), ("weekend",), ("bus", "deliver"), ("shop", "business"), ("trial", "review", "six months")]),
    ("Sleep helps the brain store memories. During deep sleep, the brain replays patterns of activity from the day, which strengthens "
     "connections linked to new skills and facts. Studies in which volunteers learned word lists found better recall after a night of "
     "sleep than after the same hours awake. Short naps can give a smaller benefit.",
     30, [("sleep",), ("memor", "recall"), ("replay", "strengthen", "deep sleep"), ("stud", "volunteer", "experiment"), ("nap",)]),
    ("A new library opened in the old railway station of Harlen. The building keeps its iron roof and clock tower, while the former "
     "ticket hall now holds 40,000 books and a children's corner. The project cost 6 million euros, mostly paid by the region. "
     "Opening hours are longer than in the previous library, including Sunday afternoons.",
     35, [("library",), ("station", "railway"), ("roof", "clock", "historic"), ("book", "40,000", "40000"), ("sunday", "hours"), ("6 million", "cost")]),
    ("Honey never really spoils because it contains little water and is slightly acidic, conditions in which most bacteria cannot "
     "survive. Bees also add an enzyme that produces small amounts of hydrogen peroxide. Archaeologists have found pots of honey in "
     "ancient tombs that were still edible. Honey can crystallise, but gentle warming makes it liquid again.",
     35, [("spoil", "last", "keep"), ("water", "moisture"), ("acid",), ("bacteria", "microb"), ("tomb", "ancient", "archaeolog"), ("crystal",)]),
    ("Electric scooters for hire arrived in the city last year. Riders unlock them with an app and pay by the minute. Hospitals "
     "reported more injuries, often from riders without helmets, and pedestrians complained about scooters left on pavements. The "
     "city now limits speed to 20 km/h and requires parking in marked zones.",
     35, [("scooter",), ("app", "hire", "rent"), ("injur", "helmet"), ("pavement", "sidewalk", "pedestrian"), ("20", "speed"), ("park", "zone")]),
    ("Glaciers in the Alps lost about a tenth of their remaining ice in two hot summers. Scientists measure the loss with stakes "
     "drilled into the ice and with satellite images. Melting glaciers raise the risk of rockfalls and change the water supply of "
     "valleys that rely on meltwater in late summer.",
     30, [("glacier",), ("alps",), ("tenth", "10", "ice loss", "lost"), ("stake", "satellite", "measure"), ("rockfall", "water supply", "meltwater")]),
    ("A bakery in Porto started selling yesterday's bread at half price in the evening through a phone app. Within a year, it threw "
     "away 70 percent less bread. Other shops in the street joined the scheme, and the city now promotes it as a way to cut food waste.",
     30, [("bakery", "bread"), ("half price", "discount", "cheaper"), ("app",), ("70",), ("waste",), ("other shops", "city", "joined")]),
    ("Octopuses have three hearts and blue blood. Two hearts pump blood through the gills, and the third pumps it to the rest of the "
     "body. Their blood uses a copper-based molecule, haemocyanin, which carries oxygen well in cold water with little oxygen.",
     30, [("octopus",), ("three hearts", "3 hearts", "hearts"), ("blue",), ("gill",), ("copper", "haemocyanin", "hemocyanin"), ("oxygen",)]),
    ("The school replaced its paper timetable with an online calendar. Teachers can now publish changes instantly, and parents receive "
     "a notification when a lesson is cancelled. Some families without reliable internet asked for printed copies, so the office still "
     "prints a weekly version on Fridays.",
     30, [("school",), ("online", "calendar", "digital"), ("teacher", "change"), ("parent", "notif"), ("internet", "print")]),
    ("Desert locusts usually live alone, but after heavy rains their numbers can grow quickly. When crowded, they change colour and "
     "behaviour and form swarms that travel up to 150 km a day, eating crops on the way. Early warning systems track rainfall and "
     "send teams to spray breeding areas before swarms form.",
     35, [("locust",), ("rain",), ("swarm",), ("crop",), ("150", "travel"), ("warning", "spray", "track")]),
    ("A small island in Denmark produces more renewable electricity than it uses, mainly from wind turbines owned by local residents. "
     "Heating comes from straw and solar plants, and electric ferries are being tested. The island earns money by selling surplus "
     "power to the mainland grid.",
     30, [("island",), ("renewable", "wind"), ("resident", "local", "communit"), ("straw", "solar", "heat"), ("ferr",), ("sell", "surplus", "grid")]),
    ("Researchers taught crows to swap tokens for food and found that the birds waited for a better token instead of taking a smaller "
     "reward at once. The ability to plan and delay a reward was long thought to be limited to humans and great apes.",
     30, [("crow",), ("token",), ("wait", "delay", "patien"), ("plan",), ("human", "ape")]),
    ("The old harbour crane, built in 1890, will be restored instead of demolished after residents collected 5,000 signatures. The "
     "restoration will repair its wooden arm and steel gears, and the crane will become a small museum about the port's history.",
     30, [("crane",), ("1890", "old", "historic"), ("restor",), ("resident", "signature", "petition"), ("museum",)]),
]

TRANSLATION_REFS = {  # V1 source sentence in each V1 language (reference translations, written for this benchmark)
    "de": "Das Museum öffnet um neun Uhr morgens. Kinder unter zwölf Jahren haben freien Eintritt.",
    "fr": "Le musée ouvre à neuf heures du matin. Les enfants de moins de douze ans entrent gratuitement.",
    "ar": "يفتح المتحف في التاسعة صباحًا. يدخل الأطفال دون سن الثانية عشرة مجانًا.",
    "zh": "博物馆早上九点开门。十二岁以下的儿童免费入场。",
    "ru": "Музей открывается в девять утра. Дети до двенадцати лет проходят бесплатно.",
    "hi": "संग्रहालय सुबह नौ बजे खुलता है। बारह साल से कम उम्र के बच्चों का प्रवेश निःशुल्क है।",
}
LANG_NAMES = {**b1.LANGS, "es": "Spanish"}
TRANSLATIONS = [  # (lang, source, reference)
    ("de", "The library will be closed on Monday because of repairs.", "Die Bibliothek bleibt am Montag wegen Reparaturarbeiten geschlossen."),
    ("fr", "Please keep your ticket until the end of the journey.", "Veuillez conserver votre billet jusqu'à la fin du trajet."),
    ("es", "The train to Madrid leaves from platform 4 at 7:30.", "El tren a Madrid sale del andén 4 a las 7:30."),
    ("ar", "The river Nile is about 6,650 kilometres long.", "يبلغ طول نهر النيل نحو 6650 كيلومترًا."),
    ("zh", "Water boils at 100 degrees Celsius at sea level.", "在海平面上，水在摄氏100度时沸腾。"),
    ("ru", "The meeting has been moved to Thursday at three o'clock.", "Встреча перенесена на четверг на три часа."),
    ("hi", "Please drink enough water every day.", "कृपया हर दिन पर्याप्त पानी पिएँ।"),
    ("es", "Our shop is open from Monday to Saturday.", "Nuestra tienda abre de lunes a sábado."),
    ("ar", "The new school will open next September.", "ستفتح المدرسة الجديدة في سبتمبر المقبل."),
]
DIALECT_SRC = ["I want to go to the market now.", "Why didn't you call me yesterday?",
               "There is no bread at home, so I will buy some tomorrow.", "How are you? I haven't seen you for a long time.",
               "This phone is very expensive, I don't want it.", "We will travel to the sea next week.", "Where is the nearest pharmacy?",
               "My brother works in a big company and comes home late.", "What do you want to eat tonight?", "The weather today is very hot.",
               "Wait for me, I'm coming in five minutes."]
DIALECT_GEN = {
    "egy": ["اكتب ثلاث جمل باللهجة المصرية العامية (مش الفصحى) عن زحمة المواصلات الصبح. اكتب الجمل فقط.",
            "اكتب رسالة قصيرة باللهجة المصرية العامية لصاحبك تعزمه على الغدا يوم الجمعة. اكتب الرسالة فقط."],
    "gulf": ["اكتب ثلاث جمل باللهجة الخليجية العامية (مو الفصحى) عن الزحمة في الطريق الصبح. اكتب الجمل فقط.",
             "اكتب رسالة قصيرة باللهجة الخليجية العامية لربعك تعزمهم على الغدا يوم الجمعة. اكتب الرسالة فقط."],
}
DIALECT_NAME = {"egy": "colloquial Egyptian Arabic (not Modern Standard Arabic)",
                "gulf": "colloquial Gulf Arabic (Emirati/Saudi, not Modern Standard Arabic)"}

TITLES = [  # (topic, keyword, count, max_chars, allowed numbers)
    ("why the sea is salty", "sea", 3, 60, set()), ("how volcanoes form islands", "volcano", 3, 60, set()),
    ("the history of the bicycle", "bicycle", 3, 55, set()), ("saving money on groceries", "groceries", 4, 60, set()),
    ("how octopuses use their three hearts", "octopus", 3, 60, {"3"}), ("learning a language in 30 days", "language", 3, 60, {"30"}),
    ("why cats purr", "cats", 5, 50, set()), ("building a home battery system", "battery", 3, 60, set()),
]
HOOKS = [  # (facts, keyword, max_words)
    ("Honey found in ancient Egyptian tombs was still edible after about 3,000 years.", "honey", 20),
    ("The Aswan High Dam was completed in 1970 and created Lake Nasser, about 500 km long.", "dam", 22),
    ("A day on Venus lasts longer than a year on Venus: 243 Earth days versus 225.", "venus", 22),
    ("Octopuses have three hearts and blue blood.", "octopus", 18),
    ("Lithium-ion battery pack prices fell about 90 percent between 2010 and 2023.", "battery", 22),
    ("The Great Barrier Reef is about 2,300 km long and visible from space.", "reef", 22),
]
BRIEFS = [
    "Topic: honeybees. Verified facts: a colony can have 20,000 to 60,000 bees; worker bees perform a waggle dance to show where flowers are; "
    "one bee makes about a twelfth of a teaspoon of honey in its life.",
    "Topic: the Aswan High Dam. Verified facts: it was completed in 1970; it created Lake Nasser; it ended the yearly Nile flood downstream.",
    "Topic: volcanoes. Verified facts: about 1,350 potentially active volcanoes exist on land; most lie along the Pacific Ring of Fire; "
    "lava can reach 1,200 degrees Celsius.",
    "Topic: sleep. Verified facts: adults need 7 to 9 hours of sleep; deep sleep helps store memories; screens before bed delay sleep.",
    "Topic: the Great Barrier Reef. Verified facts: it is about 2,300 km long; it is made of nearly 3,000 individual reefs; "
    "warm water causes coral bleaching.",
    "Topic: recycling glass. Verified facts: glass can be recycled endlessly without losing quality; recycled glass melts at a lower "
    "temperature than raw materials; sorting by colour matters.",
    "Topic: octopuses. Verified facts: they have three hearts; their blood is blue; they can change colour in under a second.",
    "Topic: the bicycle. Verified facts: the first pedal-less running machine was built in 1817; pneumatic tyres came in 1888; "
    "there are about 1 billion bicycles in the world.",
    "Topic: desert locusts. Verified facts: swarms can travel up to 150 km a day; heavy rain triggers breeding; a swarm can contain "
    "tens of millions of locusts.",
    "Topic: mangroves. Verified facts: mangrove roots reduce coastal erosion; they shelter young fish; they store more carbon per hectare "
    "than many inland forests.",
    "Topic: the Nile. Verified facts: it is about 6,650 km long; it flows north into the Mediterranean; Cairo lies on its banks.",
    "Topic: home composting. Verified facts: food scraps make up about 30 percent of household waste; compost needs air and moisture; "
    "it takes 2 to 6 months.",
    "Topic: the Moon. Verified facts: it is about 384,000 km from Earth; it always shows the same face; its gravity causes ocean tides.",
    "Topic: coffee. Verified facts: coffee beans are the seeds of a fruit; Brazil is the largest producer; caffeine takes about "
    "20 minutes to take effect.",
]
INSTRUCTIONS = [  # (prompt, checks)
    ("List exactly 4 colours of the rainbow, one per line, each line starting with a number and a full stop (1. 2. ...). Output only the list.",
     {"lines": 4, "numbered": True}),
    ("Write one sentence about rain that ends with a question mark and has at most 12 words. Output only the sentence.",
     {"lines": 1, "ends_with": "?", "max_words": 12}),
    ("Write the word 'ocean' in capital letters exactly three times, separated by single spaces. Output nothing else.",
     {"exact": "OCEAN OCEAN OCEAN"}),
    ("Answer in Russian, in exactly one sentence: what is the capital of France? Output only the sentence.",
     {"lang_script": "ru", "sentences": 1, "contains_any": ["Париж"]}),
    ("Give 3 tips for studying. Do not use the letter 'z' anywhere. Each tip on its own line starting with '* '. Output only the tips.",
     {"lines": 3, "prefix": "* ", "forbid_chars": "zZ"}),
    ("Write a sentence about trees that contains the word 'roots' exactly twice and no digits. Output only the sentence.",
     {"count_word": ["roots", 2], "no_digits": True}),
    ("Reply with only the word YES or the word NO: is 17 a prime number?", {"exact_any": ["YES", "Yes", "yes"]}),
    ("Write a haiku-style poem of exactly 3 lines about snow. Do not use the word 'white'. Output only the poem.",
     {"lines": 3, "forbid_words": ["white"]}),
    ("Translate 'good morning' into French, Spanish and German, one per line in that order, in the form 'French: ...'. Output only the 3 lines.",
     {"lines": 3, "line_prefixes": ["French:", "Spanish:", "German:"]}),
    ("Write exactly two sentences about the Moon. The second sentence must start with 'It'. Output only the two sentences.",
     {"sentences": 2, "sentence2_starts": "It"}),
    ("List 5 fruits in alphabetical order, separated by commas, all in lower case, on one line. Output only the list.",
     {"lines": 1, "csv_items": 5, "sorted_lower": True}),
    ("Write a title for a video about whales in at most 6 words, without any punctuation. Output only the title.",
     {"lines": 1, "max_words": 6, "no_punct": True}),
]


# ================================================================= task set
def taskset() -> list[dict]:
    items = []
    for it in b2.taskset():                       # V2 items unchanged (prompts identical, so V2 subsets stay comparable)
        it = dict(it)
        if it["id"].startswith("translate_") and it.get("lang"):
            it.update(kind="translation", ref=TRANSLATION_REFS[it["lang"]], src=b1.TRANSLATE_SRC)
        elif it.get("want"):
            it.update(kind="dialect", cap="dialect_" + it["want"])
        elif it["id"] == "strategy":        # same rules as V2 plus: no hook or no beats scores 0
            it.update(kind="strategy", brief=b2.BRIEF)
        elif it["id"] == "grounded_hook":   # V2 rule never checked the subject ("not stated" scored 1.0)
            it.update(kind="grounded", keyword="footbridge", allowed=["2019", "310", "48"], banned=["2020", "2023", "4", "52"])
        items.append(it)
    A = '\nReply as JSON: {"answer": "<answer>"}'
    items += [{"id": f"reason3_{i}", "cap": "reasoning", "kind": "answer", "gold": g, "schema": b1.S_ANSWER, "max_tokens": 80, "prompt": q + A}
              for i, (q, g) in enumerate(REASON3, 1)]
    for d, (doc, qa) in enumerate(long_documents(), 1):
        items += [{"id": f"longctx3_{d}_{i}", "cap": "long_context", "kind": "answer", "gold": g, "schema": b1.S_ANSWER, "max_tokens": 60,
                   "num_ctx": 8192, "prompt": f"Read the document and answer from it only.\n--- DOCUMENT ---\n{doc}\n--- END ---\nQuestion: {q}{A}"}
                  for i, (q, g) in enumerate(qa, 1)]
    items += [{"id": f"ground3_qa{i}", "cap": "grounding", "kind": "answer", "gold": g, "schema": b1.S_ANSWER, "max_tokens": 80,
               "prompt": f"Sources:\n{SRC_G[s]}\nAnswer from the sources only. {q}{A}"} for i, (s, q, g) in enumerate(GROUNDED_QA, 1)]
    items += [{"id": f"ground3_na{i}", "cap": "grounding", "kind": "unanswerable", "schema": b1.S_ANSWER, "max_tokens": 80,
               "prompt": f'Sources:\n{SRC_G[s]}\nAccording to the sources only: {q} If the sources do not say, answer "not stated".{A}'}
              for i, (s, q) in enumerate(UNANSWERABLE, 1)]
    items += [{"id": f"ground3_w{i}", "cap": "grounding", "kind": "grounded", "keyword": kw, "allowed": sorted(ok), "banned": sorted(bad),
               "max_tokens": 80, "prompt": f"Sources:\n{SRC_G[s]}\nWrite ONE sentence (max 25 words) for a short video about this {kw}. Use only "
                                           "facts that at least two sources agree on. No exaggeration. Output only the sentence."}
              for i, (s, kw, ok, bad) in enumerate(GROUNDED_WRITE, 1)]
    items += [{"id": f"conflict{i}", "cap": "conflicts", "kind": "conflict", "agree": ag, "gold": vals, "schema": S_CONFLICT, "max_tokens": 120,
               "prompt": f"Sources:\n{src}\nDo the sources agree on {q}? Reply as JSON {{\"agree\": true or false, \"values\": [every distinct "
                         "value the sources give, as strings]}."} for i, (src, q, ag, vals) in enumerate(CONFLICTS, 1)]
    items += [{"id": f"extract3_{i}", "cap": "json", "kind": "extract", "gold": g, "max_tokens": 200,
               "schema": {"type": "object", "required": list(g), "properties": {k: {} for k in g}},
               "prompt": f"Extract a JSON object with exactly these keys: {', '.join(g)}. Dates as YYYY-MM-DD, times as HH:MM, amounts as "
                         f"numbers, keep names as written.\nText: {t}"} for i, (t, g) in enumerate(EXTRACT, 1)]
    items += [{"id": f"json3_{i}", "cap": "json", "kind": "checks", "checks": c, "max_tokens": 200,
               "schema": {"type": "object", "required": req, "properties": {k: {} for k in req}}, "prompt": p}
              for i, (p, req, c) in enumerate(CHECKS, 1)]
    items += [{"id": f"summary3_{i}", "cap": "summarization", "kind": "summary", "src": t, "max_words": mw, "groups": [list(g) for g in gr],
               "max_tokens": 120, "prompt": f"Summarise the text in at most {mw} words, plain prose, no new facts.\nText: {t}"}
              for i, (t, mw, gr) in enumerate(SUMMARIES, 1)]
    items += [{"id": f"translate3_{i}_{lg}", "cap": "translation", "kind": "translation", "lang": lg, "src": s, "ref": r, "max_tokens": 160,
               "prompt": f"Translate into {LANG_NAMES[lg]}. Output only the translation.\nText: {s}"} for i, (lg, s, r) in enumerate(TRANSLATIONS, 1)]
    for want in ("egy", "gulf"):
        items += [{"id": f"dialect3_{want}_t{i}", "cap": f"dialect_{want}", "kind": "dialect", "want": want, "src": s, "max_tokens": 120,
                   "prompt": f"Translate into {DIALECT_NAME[want]}. Output only the translation.\nText: {s}"} for i, s in enumerate(DIALECT_SRC, 1)]
        items += [{"id": f"dialect3_{want}_g{i}", "cap": f"dialect_{want}", "kind": "dialect", "want": want, "max_tokens": 140, "prompt": p}
                  for i, p in enumerate(DIALECT_GEN[want], 1)]
    items += [{"id": f"titles3_{i}", "cap": "writing", "kind": "titles", "keyword": kw, "count": n, "max_chars": mc, "allowed": sorted(al),
               "schema": b1.S_TITLES, "max_tokens": 200,
               "prompt": f'Write {n} different YouTube titles for a video about {t}. Each title at most {mc} characters, must contain the word '
                         f'"{kw}", no all-caps, no clickbait, no numbers that are not in the topic. Reply as JSON {{"titles": [..]}}.'}
              for i, (t, kw, n, mc, al) in enumerate(TITLES, 1)]
    items += [{"id": f"hook3_{i}", "cap": "writing", "kind": "hook", "facts": f, "keyword": kw, "max_words": mw, "max_tokens": 80,
               "prompt": f"Fact: {f}\nWrite ONE opening sentence (max {mw} words) for a short video, using only this fact. No exaggeration, "
                         "no invented numbers. Output only the sentence."} for i, (f, kw, mw) in enumerate(HOOKS, 1)]
    items += [{"id": f"strategy3_{i}", "cap": "strategy", "kind": "strategy", "brief": br, "schema": b2.S_STRAT, "max_tokens": 350,
               "prompt": f"{br}\nPlan a 60-second explainer. Return JSON with: hook (one sentence, max 25 words, a question or a fact from the "
                         "brief, no exaggeration), beats (exactly 3 short story beats), visuals (exactly 3 concrete shots). Use only the verified facts."}
              for i, br in enumerate(BRIEFS, 1)]
    items += [{"id": f"instr3_{i}", "cap": "instruction", "kind": "constraints", "checks": c, "max_tokens": 120, "prompt": p}
              for i, (p, c) in enumerate(INSTRUCTIONS, 1)]
    return items


# ================================================================= scorers
def _answer(text: str) -> str:
    d = _json(text)
    return str(d.get("answer", "")) if isinstance(d, dict) else (text or "")


def score_conflict(it: dict, text: str) -> dict:
    d = _json(text)
    if not isinstance(d, dict):
        return {"score": 0.0}
    agree_ok = d.get("agree") is it["agree"]
    vals = " | ".join(map(str, d.get("values") or []))
    hits = [any(_has(vals, a) for a in alts) for alts in it["gold"]]
    if it["agree"]:
        distinct = {n for n in _numbers(vals)} - {n for alts in it["gold"] for a in alts for n in _numbers(a)}
        v = (1.0 if hits[0] else 0.0) * (0.5 if distinct else 1.0)
    else:
        v = sum(hits) / len(hits)
    return {"score": round(0.5 * agree_ok + 0.5 * v, 3)}


def _norm_s(x) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s:.-]", "", str(x).lower())).strip()


def _field_ok(got, want) -> bool:
    if isinstance(want, bool):
        return got is want
    if isinstance(want, (int, float)):
        try:
            return abs(float(str(got).replace(",", "")) - float(want)) < 1e-6
        except ValueError:
            return False
    if isinstance(want, list):
        return isinstance(got, list) and {_norm_s(g) for g in got} == {_norm_s(w) for w in want}
    return _norm_s(want) in _norm_s(got) and bool(_norm_s(got))


def score_extract(it: dict, text: str) -> dict:
    d = _json(text)
    if not isinstance(d, dict):
        return {"score": 0.0}
    ok = [k for k, v in it["gold"].items() if _field_ok(d.get(k), v)]
    return {"score": round(len(ok) / len(it["gold"]), 3), "missed": sorted(set(it["gold"]) - set(ok))}


def score_checks(it: dict, text: str) -> dict:
    d = _json(text)
    if not isinstance(d, dict):
        return {"score": 0.0}
    c, res = it["checks"], []
    for k, v in (c.get("eq") or {}).items():
        res.append(_field_ok(d.get(k), v) if not isinstance(v, str) else _norm_s(d.get(k)) == _norm_s(v))
    for k, n in (c.get("len") or {}).items():
        res.append(isinstance(d.get(k), list) and len(d[k]) == n)
    for k in c.get("nonempty") or []:
        res.append(isinstance(d.get(k), str) and bool(d[k].strip()))
    for k, (lo, hi) in (c.get("range") or {}).items():
        try:
            res.append(lo <= float(d.get(k)) <= hi)
        except (TypeError, ValueError):
            res.append(False)
    for k, want in (c.get("set") or {}).items():
        res.append(isinstance(d.get(k), list) and {_norm_s(x) for x in d[k]} == set(want))
    for k, mw in (c.get("max_words_each") or {}).items():
        res.append(isinstance(d.get(k), list) and bool(d[k]) and all(isinstance(x, str) and len(x.split()) <= mw for x in d[k]))
    return {"score": round(sum(res) / len(res), 3) if res else 0.0}


def score_summary(it: dict, text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {"score": 0.0}
    low = t.lower()
    cover = sum(any(a.lower() in low for a in g) for g in it["groups"]) / len(it["groups"])
    if cover == 0:                                     # says nothing from the text: length and "no new numbers" earn nothing
        return {"score": 0.0, "coverage": 0.0}
    invented = _numbers(t) - _numbers(it["src"])
    s = 0.2 * (len(t.split()) <= it["max_words"]) + 0.6 * cover + 0.2 * (not invented)
    return {"score": round(s, 3), "coverage": round(cover, 3), "invented_numbers": sorted(invented)}


def score_translation(it: dict, text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {"score": 0.0, "chrf": 0.0}
    c = b1.chrf(t, it["ref"])
    if b1.chrf(t, it["src"]) >= max(0.6, c):            # the source copied back (or left in English) is not a translation
        return {"score": 0.0, "chrf": c, "untranslated": True}
    if c < 0.15:                                       # unrelated text: script and numbers earn nothing on their own
        return {"score": 0.0, "chrf": c}
    share = b1.script_share(t, it["lang"])
    nums = {n.rstrip(".") for n in re.findall(r"\d+(?:\.\d+)?", re.sub(r"(?<=\d),(?=\d{3})", "", it["src"]))}  # written digits only
    kept = (nums <= _numbers(t)) if nums else True
    s = 0.6 * min(1.0, c / 0.6) + 0.25 * min(1.0, share / 0.9) + 0.15 * kept
    return {"score": round(s, 3), "chrf": c, "script_share": share, "numbers_kept": kept}


def _markers(text: str, words: list[str]) -> list[str]:
    toks = set(re.findall(r"[؀-ۿ]+", text or ""))
    toks |= {w[1:] for w in toks if w.startswith("و") and len(w) > 2}      # "وعايز" -> "عايز"
    return sorted(w for w in set(words) if w in toks)


def score_dialect(it: dict, text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {"score": 0.0}
    own, other = (EGY, GULF) if it["want"] == "egy" else (GULF, EGY)
    mo, mx, ms = _markers(t, own), _markers(t, other), _markers(t, MSA)
    arabic = b1.script_share(t, "ar") >= 0.8
    s = 0.2 * arabic + 0.4 * min(1.0, len(mo) / 2)
    if mo:
        s += 0.2 * (not mx) + 0.2 * (len(ms) <= 1)
    return {"score": round(s, 3), "own_markers": mo, "other_markers": mx, "msa_markers": ms}


def score_grounded(it: dict, text: str) -> dict:
    t = (text or "").strip()
    if it["keyword"].lower() not in t.lower():       # not about the subject: the negative checks earn nothing
        return {"score": 0.0}
    nums = _numbers(t)
    parts = [0 < len(t.split()) <= 25, not b2.SENSATIONAL.search(t), nums <= set(it["allowed"]), not (nums & set(it["banned"])),
             it["keyword"].lower() in t.lower()]
    return {"score": round(sum(parts) / len(parts), 3)}


def score_titles(it: dict, text: str) -> dict:
    d = _json(text)
    titles = [x for x in (d.get("titles") or []) if isinstance(x, str)] if isinstance(d, dict) else []
    if not titles:
        return {"score": 0.0}
    kw = it["keyword"].lower()
    parts = [len(titles) == it["count"], all(0 < len(x) <= it["max_chars"] for x in titles), all(kw in x.lower() for x in titles),
             len({x.lower() for x in titles}) == len(titles),
             not any(x.isupper() or "!!" in x or b2.SENSATIONAL.search(x) for x in titles),
             all(_numbers(x) <= set(it["allowed"]) for x in titles)]
    return {"score": round(sum(parts) / len(parts), 3)}


def score_hook(it: dict, text: str) -> dict:
    t = (text or "").strip()
    if it["keyword"].lower() not in t.lower():
        return {"score": 0.0}
    ends = len(re.findall(r"[.!?](?=\s|$)", t))
    parts = [0 < len(t.split()) <= it["max_words"], ends <= 1, not b2.SENSATIONAL.search(t), _numbers(t) <= _numbers(it["facts"]),
             it["keyword"].lower() in t.lower()]
    return {"score": round(sum(parts) / len(parts), 3)}


def score_strategy(it: dict, text: str) -> dict:
    d = _json(text)
    if not isinstance(d, dict):
        return {"score": 0.0}
    hook = str(d.get("hook", ""))
    beats, vis = d.get("beats") or [], d.get("visuals") or []
    if not hook.strip() or not beats:                  # no plan at all: "no invented numbers" earns nothing
        return {"score": 0.0}
    allowed = _numbers(it["brief"]) | {"60", "1", "2", "3"}
    blob = json.dumps(d, ensure_ascii=False)
    invented = _numbers(blob) - allowed
    parts = [0 < len(hook.split()) <= 25, ("?" in hook) or bool(_numbers(hook) & _numbers(it["brief"])), not b2.SENSATIONAL.search(blob),
             isinstance(beats, list) and len(beats) == 3, isinstance(vis, list) and len(vis) == 3 and all(len(str(v).split()) >= 3 for v in vis),
             not invented]
    return {"score": round(sum(parts) / len(parts), 3), "invented_numbers": sorted(invented)}


def _sentences(t: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?。])\s+", t.strip()) if s.strip()]


def score_constraints(it: dict, text: str) -> dict:
    t = (text or "").strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    c, res = it["checks"], []
    if c.get("lines", 1) > 1:                          # a one-line requirement is trivially met; it is checked below
        res.append(len(lines) == c["lines"])
    if c.get("numbered"):
        res.append(bool(lines) and all(re.match(rf"{i}\.\s", ln) for i, ln in enumerate(lines, 1)))
    if "ends_with" in c:
        res.append(t.endswith(c["ends_with"]))
    if "exact" in c:
        res.append(t == c["exact"])
    if "exact_any" in c:
        res.append(t.rstrip(".") in c["exact_any"])
    if "lang_script" in c:
        res.append(b1.script_share(t, c["lang_script"]) >= 0.9 and bool(t))
    if "sentences" in c:
        res.append(len(_sentences(t)) == c["sentences"])
    if "contains_any" in c:
        res.append(any(w in t for w in c["contains_any"]))
    if "prefix" in c:
        res.append(bool(lines) and all(ln.startswith(c["prefix"]) for ln in lines))
    if "count_word" in c:
        w, n = c["count_word"]
        res.append(len(re.findall(rf"\b{w}\b", t, re.I)) == n)
    if "line_prefixes" in c:
        res.append(len(lines) == len(c["line_prefixes"]) and all(ln.startswith(p) for ln, p in zip(lines, c["line_prefixes"])))
    if "sentence2_starts" in c:
        s = _sentences(t)
        res.append(len(s) >= 2 and s[1].startswith(c["sentence2_starts"]))
    if "csv_items" in c:
        items = [x.strip() for x in t.split(",") if x.strip()]
        res.append(len(items) == c["csv_items"])
        if c.get("sorted_lower"):
            res.append(items == sorted(items) and all(x == x.lower() for x in items))
    neg_from = len(res)
    if c.get("lines") == 1:
        res.append(len(lines) == 1)
    if "max_words" in c:
        res.append(0 < len(t.split()) <= c["max_words"])
    if "forbid_chars" in c:
        res.append(bool(t) and not any(ch in t for ch in c["forbid_chars"]))
    if c.get("no_digits"):
        res.append(bool(t) and not re.search(r"\d", t))
    if "forbid_words" in c:
        res.append(bool(t) and not any(re.search(rf"\b{w}\b", t, re.I) for w in c["forbid_words"]))
    if c.get("no_punct"):
        res.append(bool(t) and not re.search(r"[^\w\s']", t))
    if not any(res[:neg_from]):                       # nothing asked for was produced: avoiding things earns nothing
        return {"score": 0.0, "passed": 0, "checks": len(res)}
    return {"score": round(sum(res) / len(res), 3), "passed": sum(res), "checks": len(res)}


SCORERS = {"conflict": score_conflict, "extract": score_extract, "checks": score_checks, "summary": score_summary,
           "translation": score_translation, "dialect": score_dialect, "grounded": score_grounded, "titles": score_titles,
           "hook": score_hook, "strategy": score_strategy, "constraints": score_constraints}


def score_v3(it: dict, text: str) -> dict:
    k = it.get("kind")
    if k == "answer":
        a = _answer(text)
        return {"score": 1.0 if any(_has(a, g) for g in _alts(it["gold"])) else 0.0}
    if k == "unanswerable":
        a = _answer(text).lower()
        return {"score": 1.0 if (re.search(r"\b(not|unknown|no)\b", a) and not re.search(r"\d", a)) else 0.0}
    if k in SCORERS:
        return SCORERS[k](it, text)
    return b2.score_v2(it, text, None)            # V2 items keep their V2 scorer


# ================================================================= results, statistics, routing
CAPS = ["reasoning", "long_context", "grounding", "conflicts", "json", "summarization", "translation", "dialect_egy", "dialect_gulf",
        "writing", "strategy", "instruction"]
TASK_CAPS = {**b2.TASK_CAPS, "deep_research": ["reasoning", "grounding", "conflicts", "long_context"],
             "quality_review": ["reasoning", "grounding", "conflicts", "instruction"], "dialect_adaptation": ["dialect"]}


def ci95(xs: list[float]) -> tuple[float, float]:
    """Normal-approximation 95 % interval of a mean (clipped to 0..1); honest about small samples, not a significance test."""
    n = len(xs)
    if n < 2:
        return (0.0, 1.0)
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    h = 1.96 * sd / math.sqrt(n)
    return (round(max(0.0, m - h), 3), round(min(1.0, m + h), 3))


def score_results(raw: dict) -> dict:
    from collections import defaultdict
    items = {it["id"]: it for it in taskset()}
    out = {}
    for name, rec in raw["models"].items():
        rows = []
        for r in rec.get("items", []):
            it = items.get(r["id"])
            if it is None:
                continue
            text = re.sub(r"<think>.*?(</think>|$)", "", r.get("text") or "", flags=re.S)
            s = {"score": 0.0} if r.get("error") else score_v3(it, text)
            tps = (r["eval_count"] / r["eval_s"]) if r.get("eval_count") and r.get("eval_s") else None
            rows.append({"id": r["id"], "cap": it["cap"], **s, "latency_s": r.get("latency_s"), "tokens_per_s": round(tps, 1) if tps else None,
                         "thinking_chars": r.get("thinking_chars"), "error": r.get("error"), "text": text[:300]})
        caps = defaultdict(list)
        for x in rows:
            caps[x["cap"]].append(x["score"])
        caps["dialect"] = caps.get("dialect_egy", []) + caps.get("dialect_gulf", [])
        lat = [x["latency_s"] for x in rows if x.get("latency_s")]
        tps = [x["tokens_per_s"] for x in rows if x.get("tokens_per_s")]
        out[name] = {"spec": rec["spec"], "pull_ok": rec.get("pull_ok"), "pull_s": rec.get("pull_s"), "vram": rec.get("vram"),
                     "capabilities": {c: round(sum(v) / len(v), 3) for c, v in caps.items() if v},
                     "ci95": {c: ci95(v) for c, v in caps.items() if v}, "n": {c: len(v) for c, v in caps.items() if v},
                     "overall": round(sum(x["score"] for x in rows) / max(1, len(rows)), 3), "failures": sum(1 for x in rows if x.get("error")),
                     "mean_latency_s": round(sum(lat) / len(lat), 2) if lat else None,
                     "median_tokens_per_s": sorted(tps)[len(tps) // 2] if tps else None, "rows": rows}
    return out


def routing(scored: dict) -> dict:
    """V2 routing over V3 capabilities. Dialect adaptation ALWAYS requires a native reviewer, whatever it scores."""
    saved = b2.TASK_CAPS
    try:
        b2.TASK_CAPS = TASK_CAPS
        r = b2.routing(scored)
    finally:
        b2.TASK_CAPS = saved
    r["dialect_adaptation"]["requires_human_review"] = True
    for t, x in r.items():
        top = x["ranking"][:2]
        x["separable_from_next"] = None
        if len(top) == 2:
            lo = [scored[top[0]["model"]]["ci95"].get(c, (0, 1))[0] for c in x["capabilities"]]
            hi = [scored[top[1]["model"]]["ci95"].get(c, (0, 1))[1] for c in x["capabilities"]]
            x["separable_from_next"] = all(a > b for a, b in zip(lo, hi))   # True only when every interval is disjoint
    return r


def kernel_source(model_ids: list[str]) -> str:
    items = [{k: v for k, v in it.items() if k in ("id", "prompt", "schema", "max_tokens", "num_ctx")} for it in taskset()]
    models = {m: b2.MODELS[m] for m in model_ids}
    return (b2.KERNEL_TEMPLATE.replace("__ITEMS__", repr(json.dumps(items, ensure_ascii=False)))
            .replace("__MODELS__", repr(json.dumps(models))))


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="bench_v3")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("kernel")
    k.add_argument("--models", required=True)
    k.add_argument("--out", required=True)
    k.add_argument("--slug", required=True)
    k.add_argument("--user", default="ahmedhanimohamed")
    sub.add_parser("count")
    j = sub.add_parser("account1", help="write Account 1 batch_llm jobs (one per model, prompts inline); submits nothing")
    j.add_argument("--models", required=True)
    j.add_argument("--run-id", required=True)
    j.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "account1":
        plan = b2.account1_manifest_plan(a.run_id, a.models.split(","), taskset())
        Path(a.out).write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
        print(len(plan["jobs"]), "jobs;", "not eligible:", plan["not_eligible"] or "none")
        return 0
    if a.cmd == "count":
        from collections import Counter
        print(json.dumps(Counter(it["cap"] for it in taskset()), indent=1), len(taskset()))
        return 0
    ids = a.models.split(",")
    d = Path(a.out)
    d.mkdir(parents=True, exist_ok=True)
    (d / "bench.py").write_text(kernel_source(ids), encoding="utf-8")
    (d / "kernel-metadata.json").write_text(json.dumps({"id": f"{a.user}/{a.slug}", "title": a.slug, "code_file": "bench.py", "language": "python",
                                                        "kernel_type": "script", "is_private": True, "enable_gpu": True, "enable_tpu": False,
                                                        "enable_internet": True, "dataset_sources": [], "competition_sources": [],
                                                        "kernel_sources": [], "model_sources": []}, indent=1), encoding="utf-8")
    print(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
