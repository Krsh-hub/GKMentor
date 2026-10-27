from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field

class Relevance(BaseModel):
    keep: bool
    score: int = Field(ge=1, le=10)
    sector: str
    lenses: List[str] = []
    reason: str = ""

class BatchItem(BaseModel):
    id: int
    event_age_days: Optional[int] = None      # how many days ago the EVENT itself happened (0 = today); None = cannot tell
    keep: bool
    score: int = Field(ge=1, le=10)
    sector: str
    lenses: List[str] = []

class RelevanceBatch(BaseModel):
    items: List[BatchItem]

class Claim(BaseModel):
    text: str                      # one checkable fact
    kind: Literal["name", "number", "date", "place", "rank", "scheme", "other"] = "other"
    source_sentence: str           # exact sentence from the article that supports it

class Note(BaseModel):
    headline: str
    sector: str
    sub_topic: str = ""
    bullets: List[str]
    exam_angle: str
    static_links: List[str] = []
    question_angles: List[str] = []
    importance: Literal["High", "Medium", "Low"]
    lens_points: Dict[str, str] = {}   # one line per applicable exam lens
    claims: List[Claim]

class MCQ(BaseModel):
    question: str
    options: List[str]
    answer_index: int
    explanation: str
    difficulty: Literal["easy", "medium", "hard"] = "medium"

class QuizSet(BaseModel):
    mcqs: List[MCQ]

class SolverAnswer(BaseModel):
    chosen_index: int
    confident: bool

class SolverItem(BaseModel):
    id: int
    chosen_index: int
    confident: bool

class SolverBatch(BaseModel):
    answers: List[SolverItem]

class GDTopic(BaseModel):
    topic: str
    context: str
    for_points: List[str]
    against_points: List[str]
    key_facts: List[str]
    opening: str
    conclusion: str

class EditorialDraft(BaseModel):
    title: str
    body_md: str

class EventGroups(BaseModel):
    groups: List[List[int]]

class BatchNote(Note):
    event: int

class NoteBatch(BaseModel):
    notes: List[BatchNote]

class RateItem(BaseModel):
    value: float
    quote: str

class RateStatement(BaseModel):
    is_rate_announcement: bool
    announced_on: Optional[str] = None
    rates: Dict[str, RateItem] = {}

class DraftRows(BaseModel):
    rows: List[Dict[str, str]]
