"""A text mock interview (beta): one question at a time, then written feedback and a stronger version of your answer.

Like the prep tools, it works only from the person's documents. The stronger version may only rearrange and sharpen what the
person wrote or what the documents state; where it would need a fact nobody gave, it leaves a visible blank to fill in."""
from __future__ import annotations

import time

import candidate
import prep

TYPES = ("Behavioral", "Technical")
MAX_QUESTIONS = 8

ASK_TASK = {
    "Behavioral": "a behavioral question (a past situation: conflict, ownership, failure, leadership, ambiguity, teamwork), "
                  "chosen so that the person's documents contain something real to answer with",
    "Technical": "a technical question for this role: {tech}",
}
TECH = {
    "Academic": "about their own research, methods and publications, or a method the job description names; start open and let follow-ups go deeper",
    "Industry": "about a skill, tool or method the job description names, ideally one the documents show they have used, or a short design or reasoning problem for the role",
}


def question_prompt(role: candidate.Role, kind: str, asked: list[str]) -> tuple[str, str]:
    task = ASK_TASK[kind].format(tech=TECH.get(role.kind, TECH["Industry"]))
    done = "\n".join(f"- {q}" for q in asked) or "(none yet)"
    system = prep.GROUND + "\n\nYou are now the interviewer in a mock interview. Stay in that role."
    user = (f"{prep.context(role)}\n\nMOCK QUESTION. Ask {task}.\nQuestions already asked, do not repeat them:\n{done}\n\n"
            "Reply with the single question only, as you would say it aloud. No preface, no hints, no answer.")
    return system, user


def feedback_prompt(role: candidate.Role, kind: str, question: str, answer: str) -> tuple[str, str]:
    system = prep.GROUND + "\n\nYou are now an interviewing coach giving honest feedback on one answer. No scores or grades."
    user = (f"{prep.context(role)}\n\nFEEDBACK on a {kind.lower()} answer.\nQUESTION: {question}\n\nTHE PERSON'S ANSWER:\n{answer}\n\n"
            "Write exactly these sections:\n"
            "## What worked\nSpecific things, quoting their words.\n"
            "## What to improve\nThe most important two or three points: structure, specificity, missing result or numbers, length, "
            "claims their documents do not support.\n"
            "## What your documents could add\nReal experience from their documents that would strengthen this answer, with its tag, "
            "or a GAP: line if nothing fits.\n"
            "## A stronger version\nRewrite their answer in their voice, first person, spoken style. Use only facts from their answer and "
            "their documents. Where a number or detail is needed and nobody gave it, write [add: what is needed] instead of inventing it.")
    return system, user


def _ask(complete, system: str, user: str, timeout: int) -> str:
    if complete is None:
        raise RuntimeError("This needs Claude through your Claude Code login (the `claude` command). It is not available.")
    complete = getattr(complete, "complete", complete)
    out = complete(system, user, timeout=timeout)
    text = (out.text or "").strip()
    if not text:
        raise RuntimeError("The model returned nothing.")
    return text


def next_question(complete, role: candidate.Role, kind: str, asked: list[str], timeout: int = 300) -> str:
    return _ask(complete, *question_prompt(role, kind, asked), timeout=timeout)


def feedback(complete, role: candidate.Role, kind: str, question: str, answer: str, timeout: int = 600) -> str:
    if not answer.strip():
        raise ValueError("Write an answer first.")
    return _ask(complete, *feedback_prompt(role, kind, question, answer), timeout=timeout)


# ---------------------------------------------------------------- the saved record
def record(role: candidate.Role, kind: str, started: str, items: list[dict]) -> None:
    """Keep the whole interview as one Markdown file in the role's prep folder, so you can read it again."""
    lines = [f"# Mock interview ({kind.lower()}), {started}", ""]
    for i, it in enumerate(items, start=1):
        lines += [f"## Question {i}", it["question"], "", "### Your answer", it.get("answer", "(not answered)"), ""]
        if it.get("feedback"):
            lines += ["### Feedback", it["feedback"], ""]
    candidate._write(prep.prep_dir(role) / f"mock_{started.replace(':', '').replace(' ', '_')}.md", "\n".join(lines))


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")
