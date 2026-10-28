import os, yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Load .env file if present
_env_path = ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _k, _v = _k.strip(), _v.strip().strip("'\"")
            if _k and _k not in os.environ:
                os.environ[_k] = _v

DB_PATH = os.getenv("GK_DB", str(ROOT / "gk.db"))
OUT_DIR = Path(os.getenv("GK_OUT", str(ROOT / "out")))
PROMPT_VERSION = "v1"
RELEVANCE_THRESHOLD = int(os.getenv("GK_RELEVANCE_THRESHOLD", "6"))
# Shadow mode: nothing is published without human approval until you flip this on.
AUTO_PUBLISH = os.getenv("GK_AUTO_PUBLISH", "0") == "1"
MAX_NOTES_PER_DAY = int(os.getenv("GK_MAX_NOTES", "20"))
DAILY_LLM_CALL_CAP = int(os.getenv("GK_CALL_CAP", "400"))  # cost guard
CLUSTER_SIM = 0.5
STATIC_TIER = os.getenv("GK_STATIC_TIER", "cheap")
STATIC_AUTOFILL = int(os.getenv("GK_STATIC_AUTOFILL", "2"))   # AI draft batches added to the Static GK review queue per daily run (0 = off)
RATES_AUTO = os.getenv("GK_RATES_AUTO", "1") != "0"      # apply verified RBI rate announcements automatically (0 = always ask me first)
NOTE_BATCH = int(os.getenv("GK_NOTE_BATCH", "3"))      # events written per AI request (saves daily quota)
CLUSTER_WINDOW_DAYS = int(os.getenv("GK_CLUSTER_DAYS", "2"))   # only merge a story with stories from the last N days
EVENT_MERGE_DAYS = 10                                        # AI duplicate check also looks at notes from the last N days
ED_TIER = os.getenv("GK_ED_TIER", "strong")
GD_TIER = os.getenv("GK_GD_TIER", "cheap")
GD_PER_RUN = int(os.getenv("GK_GD_PER_RUN", "3"))
MAX_AGE_DAYS = int(os.getenv("GK_MAX_AGE_DAYS", "2"))      # only today's and yesterday's reports (1 = today only)
MAX_EVENT_AGE_DAYS = int(os.getenv("GK_MAX_EVENT_AGE", "3"))  # drop stories whose EVENT is older than this, even if republished today
QUIZ_TIER = os.getenv("GK_QUIZ_TIER", "cheap")          # quizzes on the cheap model keep the strong model's daily quota for notes
QUIZ_PER_NOTE = int(os.getenv("GK_QUIZ_PER_NOTE", "3"))
MAX_ARTICLES_PER_RUN = int(os.getenv("GK_MAX_ARTICLES", "300"))   # score at most this many new articles per run
RELEVANCE_BATCH = int(os.getenv("GK_RELEVANCE_BATCH", "10"))      # articles per relevance call (fewer API requests)

def load_sources(path=None):
    path = Path(path) if path else ROOT / "config" / "sources.yaml"
    return yaml.safe_load(Path(path).read_text())

def load_lenses():
    return yaml.safe_load((ROOT / "config" / "lenses.yaml").read_text())["lenses"]
