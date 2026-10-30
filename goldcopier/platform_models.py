"""Validated API payloads. No unrestricted JSON-to-broker pass-through."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Credentials(Model):
    email: str = Field(
        min_length=5, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    )
    password: str = Field(min_length=12, max_length=128)


class Signup(Credentials):
    name: str = Field(min_length=2, max_length=80)


class AccountInput(Model):
    name: str = Field(min_length=2, max_length=60)
    broker: Literal["Exness", "PrimeXBT", "Other MT5"]
    login: str = Field(pattern=r"^\d{3,20}$")
    server: str = Field(min_length=2, max_length=100)
    symbol: str = Field(default="XAUUSD", pattern=r"^XAUUSD[A-Za-z0-9._-]{0,12}$")
    mode: Literal["demo", "live"] = "demo"
    hosting: Literal["self", "managed"] = "self"
    analyst_id: str | None = Field(default=None, min_length=1, max_length=64)
    password: str = Field(default="", max_length=128)
    consent: Literal[True]


class Risk(Model):
    sizing: Literal["percent", "cash", "fixed"] = "percent"
    risk_pct: float = Field(default=0.5, gt=0, le=2)
    cash_risk: float = Field(default=10, gt=0, le=10000)
    fixed_lots: float = Field(default=0.01, gt=0, le=10)
    max_lots: float = Field(default=0.1, gt=0, le=10)
    daily_loss_pct: float = Field(default=3, gt=0, le=10)
    drawdown_pct: float = Field(default=10, gt=0, le=30)
    total_risk_pct: float = Field(default=3, gt=0, le=10)
    max_open: int = Field(default=2, ge=1, le=10)
    max_spread: float = Field(default=0.5, gt=0, le=20)
    max_entry_distance: float = Field(default=0.5, gt=0, le=20)
    tp1_close_pct: float = Field(default=50, ge=0, le=100)
    tp2_close_pct: float = Field(default=50, ge=0, le=100)
    be_enabled: bool = True
    respect_manual_stops: bool = True

    @model_validator(mode="after")
    def allocations(self):
        if self.tp1_close_pct + self.tp2_close_pct > 100:
            raise ValueError(
                "TP allocations must total at most 100%; remainder is the runner"
            )
        return self


class Follow(Model):
    analyst_id: str = Field(min_length=1, max_length=64)


class Analyst(Model):
    name: str = Field(min_length=2, max_length=60)
    description: str = Field(default="", max_length=300)
    guild_id: str = Field(pattern=r"^\d{1,24}$")
    channel_id: str = Field(pattern=r"^\d{1,24}$")
    trader_id: str = Field(pattern=r"^\d{1,24}$")
    active: bool = True


class Control(Model):
    action: Literal["breakeven", "close", "partial", "cancel", "modify"]
    position: int | None = Field(default=None, gt=0)
    signal_id: str | None = Field(default=None, max_length=100)
    percent: float | None = Field(default=None, gt=0, le=100)
    sl: float | None = Field(default=None, gt=0)
    tp: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_control(self):
        if self.position is None and not self.signal_id:
            raise ValueError("A position ticket or signal reference is required")
        if self.action == "partial" and self.percent is None:
            raise ValueError("Partial close requires percent")
        if self.action == "modify" and self.sl is None and self.tp is None:
            raise ValueError("Provide SL or TP")
        return self


class Snapshot(Model):
    login: str
    server: str
    mode: Literal["demo", "live"]
    balance: float = Field(ge=0)
    equity: float = Field(ge=0)
    currency: str = Field(max_length=12)
    positions: list[dict] = Field(default_factory=list, max_length=100)
    orders: list[dict] = Field(default_factory=list, max_length=100)
    deals: list[dict] = Field(default_factory=list, max_length=100)
    daily_pnl: float = 0
    error: str | None = Field(default=None, max_length=300)


class JobResult(Model):
    state: Literal["executed", "rejected", "uncertain"]
    detail: str = Field(max_length=500)
    ticket: int | None = None


class Toggle(Model):
    enabled: bool


class Ingest(Model):
    analyst_id: str
    source_id: str = Field(min_length=1, max_length=100)
    text: str = Field(max_length=4000)
    created: float


class Resolve(Model):
    state: Literal["executed", "rejected"]
    note: str = Field(min_length=12, max_length=500)


class Checkout(Model):
    plan: Literal["basic", "pro"]


class ResetRequest(Model):
    email: str = Field(max_length=254)


class ResetPassword(Model):
    token: str = Field(min_length=32, max_length=128)
    password: str = Field(min_length=12, max_length=128)


class AutoResult(Model):
    id: str = Field(pattern=r"^auto-[0-9a-f]{32}$")
    signal_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result: JobResult
