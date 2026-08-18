# Two connection builders, one per identity: the owner role that provisions, and the read-only
# agent role that everything on the agent side of the project must use.
# Exists so the privilege boundary is a fact about which function you called, not a convention
# someone has to remember - there is no code path that hands the agent the owner's credentials.
# Both read POSTGRES_* / AGENT_DB_* from the environment, the same contract the loader uses.

from __future__ import annotations

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from . import config as cfg

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env():
    load_dotenv(os.path.join(REPO_ROOT, ".env"))


def _require(*keys):
    _load_env()
    missing = [key for key in keys if not os.getenv(key)]
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")
    return {key: os.getenv(key) for key in keys}


def _url(user, password):
    """URL.create escapes the credentials, so a password containing @ or : cannot corrupt the
    DSN - the same reason the Day 2 loader builds its URL this way rather than by formatting."""
    _load_env()
    return URL.create(
        drivername="postgresql+psycopg2",
        username=user,
        password=password,
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB"),
    )


def agent_credentials():
    values = _require(cfg.AGENT_ROLE_ENV_USER, cfg.AGENT_ROLE_ENV_PASSWORD, "POSTGRES_DB")
    return values[cfg.AGENT_ROLE_ENV_USER], values[cfg.AGENT_ROLE_ENV_PASSWORD]


def build_agent_engine(**kwargs):
    """The only connection the agent side is allowed to open. SELECT on analytics, INSERT on the
    audit table, nothing else - and no code path here can widen that, because the privileges live
    in the database rather than in this function."""
    user, password = agent_credentials()
    return create_engine(_url(user, password), future=True, **kwargs)


def build_owner_engine(**kwargs):
    """The provisioning identity (revenue_ops). Used by provision.py to create the role and by
    the test harness to read the audit trail back - never by agent code."""
    values = _require("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    return create_engine(
        _url(values["POSTGRES_USER"], values["POSTGRES_PASSWORD"]), future=True, **kwargs
    )
