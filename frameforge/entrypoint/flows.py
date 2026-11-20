"""Question-driven flows shared by the CLI and the hub.

A flow declares its ``Question``s as data. ``Prompter`` asks them on a terminal;
``Answers`` serves them from a dict the hub posted. The server exposes a flow's
questions (``describe``) so the hub can render the same form with the same wording.
"""

import getpass
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

Validator = Callable[[str], str | None]   # returns a problem, or None when the value is fine


@dataclass(frozen=True, slots=True)
class Question:
    key: str
    label: str
    help: str = ""
    default: str = ""
    secret: bool = False
    repeat: bool = False                  # ask twice and require a match (passwords)
    choices: tuple[str, ...] = ()
    describe: dict = field(default_factory=dict)
    validate: Validator | None = field(default=None, compare=False)


class Prompter:
    """Terminal: asks each question, re-asks on a validation problem."""

    def ask(self, question: Question) -> str:
        while True:
            value = self._read(question)
            if question.repeat and value != self._read(question, again=True):
                self.say("values did not match")
                continue
            problem = _check(question, value)
            if problem is None:
                return value
            self.say(problem)

    def confirm(self, text: str) -> bool:
        return input(f"{text} [y/N]: ").strip().lower() in ("y", "yes")

    def say(self, text: str) -> None:
        print(text)

    def _read(self, question: Question, again: bool = False) -> str:
        label = "again" if again else question.label
        if question.choices and not again:
            for number, choice in enumerate(question.choices, start=1):
                self.say(f"  {number}. {question.describe.get(choice, choice)}")
            default = str(question.choices.index(question.default) + 1) \
                if question.default in question.choices else ""
            shown = f"{label} [{default}]: " if default else f"{label}: "
        else:
            shown = f"{label} [{question.default}]: " \
                if question.default and not question.secret else f"{label}: "
        raw = (getpass.getpass(shown) if question.secret else input(shown)).strip()
        if question.choices and raw.isdigit() and 1 <= int(raw) <= len(question.choices):
            return question.choices[int(raw) - 1]
        return raw or question.default


class Answers(Prompter):
    """Pre-filled: the hub posted every answer. A missing or invalid one is an error."""

    def __init__(self, answers: dict, assume_yes: bool = True) -> None:
        self.answers = {key: str(value) for key, value in answers.items()}
        self.assume_yes = assume_yes

    def ask(self, question: Question) -> str:
        value = self.answers.get(question.key, question.default)
        if not value and question.key not in self.answers:
            raise ValueError(f"missing answer for {question.key!r}")
        problem = _check(question, value)
        if problem is not None:
            raise ValueError(f"{question.key}: {problem}")
        return value

    def confirm(self, text: str) -> bool:
        return self.assume_yes

    def say(self, text: str) -> None:
        pass


def collect(questions: list[Question], prompter: Prompter) -> dict[str, str]:
    return {question.key: prompter.ask(question) for question in questions}


def describe(questions: list[Question]) -> list[dict]:
    """JSON-safe view for the hub: everything but the validator."""
    return [{k: v for k, v in asdict(q).items() if k != "validate"} for q in questions]


def _check(question: Question, value: str) -> str | None:
    if not value:
        return f"{question.label} is required"
    if question.choices and value not in question.choices:
        return f"{question.label} must be one of {', '.join(question.choices)}"
    if question.validate is not None:
        return question.validate(value)
    return None
