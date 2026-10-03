"""The one structured step the vision model returns per screenshot."""

from typing import Literal

from pydantic import BaseModel, Field

ACTIONS = ("click", "double_click", "type", "key", "scroll", "wait", "done", "fail")


class GuiStep(BaseModel):
    thought: str = Field(default="", max_length=400)
    action: Literal["click", "double_click", "type", "key", "scroll", "wait", "done", "fail"]
    x: int | None = None  # screenshot pixels
    y: int | None = None
    text: str | None = Field(default=None, max_length=300)
    key: str | None = Field(default=None, max_length=40)
    direction: Literal["up", "down"] | None = None
    amount: int | None = Field(default=None, ge=1, le=30)
    seconds: float | None = Field(default=None, ge=0, le=10)
    # only for action == "done": what the model read off the screen
    contact_email: str | None = Field(default=None, max_length=200)
    contact_name: str | None = Field(default=None, max_length=120)
    contact_role: str | None = Field(default=None, max_length=120)

    def signature(self) -> str:
        """Same action on the same target, for loop detection."""
        return f"{self.action}:{self.x}:{self.y}:{self.text}:{self.key}:{self.direction}"

    def to_executor(self) -> dict:
        """Body for POST /action. Only fields that belong to this action are sent."""
        a = self.action
        if a in ("click", "double_click"):
            return {"action": a, "x": self.x, "y": self.y}
        if a == "type":
            return {"action": a, "text": self.text}
        if a == "key":
            return {"action": a, "key": self.key}
        if a == "scroll":
            return {"action": a, "direction": self.direction or "down", "amount": self.amount or 5}
        return {"action": "wait", "seconds": self.seconds or 1}
