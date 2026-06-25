import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Config:
    openrouter_key: str = os.getenv("OPENROUTER_API_KEY", "")
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_repo: str = os.getenv("GITHUB_REPO", "")
    model_workhorse: str = os.getenv("PRAXIS_MODEL_WORKHORSE", "deepseek/deepseek-v4-pro")
    model_planner: str = os.getenv("PRAXIS_MODEL_PLANNER", "deepseek/deepseek-v4-flash")
    db_path: str = os.getenv("PRAXIS_DB", "data/praxis.db")

def load() -> Config:
    return Config()
