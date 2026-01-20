from pydantic import BaseModel


class MetricsResponse(BaseModel):
    accounts_handled: int
    net_returns_k: float
    net_returns_pct: float


class SummaryResponse(BaseModel):
    summary_text: str
    drilldown_text: str
