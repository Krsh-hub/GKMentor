# Version prompts: bump PROMPT_VERSION in config.py whenever you change anything here.
RELEVANCE_SYSTEM = (
    "You are an exam-content editor for Indian competitive exams (UPSC/State PSC, SSC/Banking/Railways, "
    "Defence, and management-exam business awareness). Judge whether a news item is worth studying."
)
RELEVANCE_PROMPT = """Score this item for competitive-exam value (1-10). Discard entertainment, local crime, routine sports.
Pick one sector (Polity, Economy, Banking, Environment, S&T, Defence, IR, Schemes, Awards, Sports, Appointments, Reports&Indices, Business, Other)
and list suitable lenses from: {lenses}.

TITLE: {title}
SOURCE_TIER: {tier}
ARTICLE:
{text}
"""
RELEVANCE_BATCH_PROMPT = """Today is {today}. Score EACH item below for competitive-exam value (1-10). Discard entertainment, local crime, routine sports.
For each item pick one sector (Polity, Economy, Banking, Environment, S&T, Defence, IR, Schemes, Awards, Sports, Appointments, Reports&Indices, Business, Other)
and list suitable lenses from: {lenses}. For each item also give event_age_days: how many days ago the event or announcement ITSELF happened (0 = today, 1 = yesterday), judging from the text and the published date; use null if you cannot tell.
Explainers, background pieces and repeats of an old story (for example an old rate decision) must get the true age of the event, not the date they were published.
Return exactly one entry per item, using the same id as shown in [brackets].

ITEMS:
{items}
"""
NOTE_SYSTEM = (
    "You write concise, exam-oriented current-affairs notes. Use ONLY facts present in the article. "
    "Never add facts from memory. Every claim must quote the exact supporting sentence from the article."
)
NOTE_PROMPT = """Write a study note from the article below.
- 4-7 crisp bullets (what happened + why it matters)
- exam_angle: why it matters for Prelims/Mains/other exams
- static_links: related static topics (Constitution articles, schemes, reports) - only if clearly implied
- claims: every checkable fact (names, numbers, dates, places, ranks, schemes) with the EXACT supporting sentence copied from the article
- sector: choose EXACTLY one of: {sectors}
- importance: High / Medium / Low
- if several SOURCES are given, combine them into ONE note; if they give different figures, say so and name each source
- lens_points: for each applicable lens in {lenses}, ONE line. UPSC_PSC = Prelims/Mains angle; SSC_BANKING = the single fact to memorise; DEFENCE = strategic/technical relevance; MANAGEMENT = a business/policy discussion point. Omit lenses that do not apply.

TITLE: {title}
ARTICLE:
{text}
"""
QUIZ_SYSTEM = "You write high-quality exam MCQs with exactly one defensible correct answer and plausible distractors."
QUIZ_PROMPT = """Write {n} MCQs (4 options each) from this verified note. Use only the note's facts.

HEADLINE: {headline}
BULLETS:
{bullets}
"""
SOLVER_SYSTEM = "Answer the multiple-choice question using your own knowledge only. Say if you are not confident."
SOLVER_PROMPT = """QUESTION: {question}
OPTIONS:
{options}
"""

SOLVER_BATCH_PROMPT = """Answer EACH question using your own knowledge only. Give the id, the index of the option you choose, and whether you are confident.

{questions}
"""

GD_SYSTEM = "You prepare group-discussion (GD) topics for management and interview exams. Be balanced and concrete. Use ONLY facts given."
GD_PROMPT = """Create ONE group-discussion topic from this approved note.
- topic: a debatable statement or question
- context: 2 sentences of background
- for_points: 4 distinct arguments in favour
- against_points: 4 distinct arguments against
- key_facts: facts worth quoting, copied from the note (exact numbers; never add numbers that are not below)
- opening: a strong 1-2 sentence opening line
- conclusion: a balanced 1-2 sentence closing line

HEADLINE: {headline}
NOTE:
{bullets}
VERIFIED FACTS:
{facts}
"""

GD_TOPIC_PROMPT = """The editor chose this group-discussion topic. Use it exactly as written.
TOPIC: {topic}
EDITOR NOTES (optional facts or angles to include): {extra}
VERIFIED CONTEXT from approved current-affairs notes:
{context}

Create the GD preparation:
- context: 2 sentences of neutral background
- for_points: 4 distinct arguments in favour
- against_points: 4 distinct arguments against
- key_facts: ONLY facts that appear in the editor notes or verified context above (copy numbers exactly). If there are none, return an empty list. Never invent statistics, dates or names.
- opening: a strong 1-2 sentence opening line
- conclusion: a balanced 1-2 sentence closing line
"""

ED_SYSTEM = ("You draft analytical editorials for Indian competitive-exam aspirants. Write in clear, neutral, exam-oriented prose. "
             "Use ONLY facts given to you. Never invent statistics, dates, names or quotations.")
ED_PROMPT = """Draft an editorial for the editor to review and edit.
TOPIC OR ANGLE: {topic}
EDITOR NOTES (optional facts or angles to include): {extra}
VERIFIED CONTEXT from approved current-affairs notes:
{context}

Format: Markdown, 450-650 words, blank line between paragraphs, "- " for bullets, **bold** for key terms. Structure:
background and why it matters now; the key issues with arguments on both sides; the way forward; a short conclusion. Put no headings above the first paragraph.
Return a crisp title (under 12 words) and the body.
"""

EVENT_PROMPT = """Below are news stories. Some are the SAME underlying event reported by different outlets or worded differently. Group the ids that are the same event.
Rules: group only if they report the same news (same event, same development). Do NOT group follow-ups that add a new development, different events on the same topic, or opposite outcomes.
Items marked (already covered) were published earlier; if a new story just repeats one of them, put them in one group.
Return only groups with 2 or more ids, using the ids in [brackets].

{items}
"""

NOTE_BATCH_PROMPT = """Write ONE study note for EACH event below, in the same order. Set "event" to the event number. For every note:
- 4-7 crisp bullets (what happened + why it matters); exam_angle (why it matters for Prelims/Mains/other exams)
- static_links: related static topics, only if clearly implied
- claims: every checkable fact with the EXACT supporting sentence copied from THAT event's sources
- sector: choose EXACTLY one of: {sectors}
- importance: High / Medium / Low
- if several SOURCES are given for an event, combine them into ONE note; if they give different figures, say so and name each source
- lens_points: for each applicable lens in {lenses}, ONE line (UPSC_PSC = Prelims/Mains angle; SSC_BANKING = the single fact to memorise; DEFENCE = strategic/technical relevance; MANAGEMENT = a business/policy discussion point). Omit lenses that do not apply.
Use ONLY facts present in each event's own sources. Never mix facts between events.

{events}
"""

RATE_SYSTEM = "You read official Reserve Bank of India statements and extract policy rates exactly as stated. Never infer, compute or guess a value."
RATE_PROMPT = """Decide whether the text below ANNOUNCES or CONFIRMS current RBI policy rates. Set is_rate_announcement to false if it does not state current rate values (for example an auction notice or a survey release).
For every rate the text states as in force after this announcement (new OR unchanged), add an entry to "rates" using ONLY these keys:
repo_rate, reverse_repo_rate, sdf_rate, msf_rate, bank_rate, crr, slr
- value: the number in per cent (5.5 for "5.50 per cent")
- quote: the EXACT sentence from the text that states it
Include a rate only if the text states its value. announced_on: the announcement date as YYYY-MM-DD if the text gives it, else null.

TEXT:
{text}
"""
DRAFT_SYSTEM = "You prepare reference data for Indian competitive-exam aspirants. Only include well-established facts found in standard exam books. If unsure about a value, leave it empty. Never guess."
DRAFT_PROMPT = """Draft {n} NEW entries for the category "{name}". Columns: {cols}. Focus: {hint}.
Return rows as objects with exactly those column names. Leave a column as "" if you are not certain. For glossary categories keep definitions concise and ALWAYS leave current_value empty.
Guidance for this category: {guide}
Do not repeat any of these existing entries: {existing}
"""
EXTRACT_PROMPT = """Extract rows for the category "{name}" from the OFFICIAL TEXT below. Columns: {cols}.
Use only what the text states; copy numbers exactly; leave a column "" if the text does not give it.

OFFICIAL TEXT:
{text}
"""
