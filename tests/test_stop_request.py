"""Stop: a stopped question is never shown, saved or remembered; a normal one is unchanged."""
import threading
import time

from jobs import Job
from sessions import Session


def no_files(monkeypatch):
    saves = []
    monkeypatch.setattr(Session, "save", lambda self: saves.append(1))
    return saves


# ---------------------------------------------------------------- the background job
def test_job_returns_the_result_and_is_not_stopped():
    j = Job("1", lambda cancel: "answer")
    for _ in range(100):
        if j.done():
            break
        time.sleep(0.01)
    assert j.done() and j.result == "answer" and j.error is None and not j.stopped


def test_job_keeps_an_error_for_the_page():
    def boom(cancel):
        raise ValueError("no")
    j = Job("2", boom)
    for _ in range(100):
        if j.done():
            break
        time.sleep(0.01)
    assert isinstance(j.error, ValueError)


def test_stop_sets_the_flag_the_function_can_see():
    seen = []
    go = threading.Event()

    def fn(cancel):
        go.wait(2)
        seen.append(cancel.is_set())
    j = Job("3", fn)
    j.stop()
    go.set()
    for _ in range(100):
        if j.done():
            break
        time.sleep(0.01)
    assert seen == [True] and j.stopped


# ---------------------------------------------------------------- the engine
def test_a_question_that_is_not_stopped_is_saved_as_before(engine, monkeypatch):
    saves = no_files(monkeypatch)
    sess = Session(id="s1")
    res = engine.ask("python pytorch requirements", mode="claude", session=sess, cancel=threading.Event())
    assert res.answers and not res.cancelled and len(sess.turns) == 1 and saves


def test_stopped_before_the_call_sends_nothing_and_saves_nothing(engine, providers, monkeypatch):
    saves = no_files(monkeypatch)
    sess = Session(id="s2")
    cancel = threading.Event()
    cancel.set()
    res = engine.ask("python pytorch requirements", mode="claude", session=sess, cancel=cancel)
    assert res.cancelled and not res.answers
    assert not providers["claude"].calls, "nothing may be sent after Stop"
    assert not sess.turns and not saves


def test_stopped_while_the_model_is_answering_discards_the_answer(engine, providers, monkeypatch):
    saves = no_files(monkeypatch)
    sess = Session(id="s3")
    cancel = threading.Event()
    original = providers["claude"].generate

    def slow(*a, **k):
        cancel.set()                    # Stop is pressed while the call is running
        return original(*a, **k)
    monkeypatch.setattr(providers["claude"], "generate", slow)
    res = engine.ask("python pytorch requirements", mode="claude", session=sess, cancel=cancel)
    assert res.cancelled and not res.answers and any("Stopped" in n for n in res.notes)
    assert not sess.turns and not saves, "a stopped answer must not reach the chat memory"
