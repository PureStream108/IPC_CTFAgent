from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.deps import get_state
from backend.competition.store import CompetitionConflict
from backend.ops.questions import QuestionStore

router = APIRouter(prefix="/questions", tags=["questions"])


class Answer(BaseModel):
    session_id: str
    value: str = Field(min_length=1, max_length=16384)


@router.get("")
def pending_questions(session_id: str, state=Depends(get_state)):
    return {"questions": QuestionStore(state).list(session_id)}


@router.post("/{question_id}/answer")
def answer_question(question_id: str, body: Answer, state=Depends(get_state)):
    try:
        return QuestionStore(state).answer(question_id, body.session_id, body.value)
    except KeyError as exc:
        raise HTTPException(404, "Question not found") from exc
    except CompetitionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
