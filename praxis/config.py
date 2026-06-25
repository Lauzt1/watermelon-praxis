import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Config:
    openrouter_key: str = os.getenv("OPENROUTER_API_KEY", "")
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_repo: str = os.getenv("GITHUB_REPO", "")
    model_workhorse: str = os.getenv("PRAXIS_MODEL_WORKHORSE", "z-ai/glm-4.6")
    model_planner: str = os.getenv("PRAXIS_MODEL_PLANNER", "openai/gpt-oss-120b")
    db_path: str = os.getenv("PRAXIS_DB", "data/praxis.db")

def load() -> Config:
    return Config()
