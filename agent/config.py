# Which LLM, how many turns, how big a brief - separate from the guardrail boundary in
# agent/guardrails/config.py, which this file must never reach into.
# Exists so a provider switch is a value change here plus an adapter under agent/llm/, and the
# investigation logic, the tool, and every Day 7 guardrail stay untouched.

import os

from dotenv import load_dotenv

# CLAUDE.md's third gotcha, now applying to API keys as well as the database: `.env` is a file,
# not the process environment, and every vendor SDK reads its key from the latter. They defer
# that read to request time, so a missing key does not fail at construction - it fails seconds
# into a run. Loading here, in the module every agent entry point imports, closes that gap.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_REPO_ROOT, ".env"))

# Sixth gotcha, local to this machine: AVG Antivirus intercepts TLS and re-signs every HTTPS
# response with its own root CA. Windows trusts that root, so browsers are fine, but Python
# verifies against certifi's public-CA bundle and fails with CERTIFICATE_VERIFY_FAILED. The fix
# is to widen the trust bundle, never to disable verification - an unverified connection would
# be carrying the API key. certs/build_ca_bundle.py writes certifi + the Windows roots to a
# gitignored file; if it is absent (any normal machine) nothing here applies.
_CA_BUNDLE = os.path.join(_REPO_ROOT, "certs", "combined-ca-bundle.pem")
if os.path.exists(_CA_BUNDLE):
    os.environ.setdefault("SSL_CERT_FILE", _CA_BUNDLE)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _CA_BUNDLE)

# --- Provider ---------------------------------------------------------------------------
# Groq, on its free tier. The project was built against Anthropic and the Anthropic adapter is
# still present and working; the switch is a cost decision, documented in CLAUDE.md's LOCKED
# SCOPE rather than quietly applied. `agent/llm/` is what makes it a one-line change.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq")

_MODELS = {
    # llama-3.3-70b-versatile was the intended model and Groq has retired it - the account 404s
    # on it. gpt-oss-120b is the largest tool-calling text model Groq still serves for free, so
    # it is the closest match to the intent: root-cause attribution needs a model that can hold a
    # hypothesis, check a lead-lag relationship and rule an alternative out. Verified against the
    # live models endpoint; qwen3.6-27b was tested and would not emit a tool call at all.
    "groq": "openai/gpt-oss-120b",
    "anthropic": "claude-opus-5",
    "openai-compatible": os.getenv("LLM_MODEL", ""),
}
MODEL = os.getenv("LLM_MODEL") or _MODELS.get(LLM_PROVIDER, "")

# Only read by the generic openai-compatible provider, for a vendor that has no adapter of its
# own. Groq's URL lives in the adapter because Groq is a named, supported provider.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY_ENV = os.getenv("LLM_API_KEY_ENV", "LLM_API_KEY")

# Anthropic-only knobs, ignored by every other adapter. Kept so switching back is a value change.
THINKING = {"type": "adaptive"}
EFFORT = "high"

# Sized by Groq's free-tier rate limit, not by how long a brief is. That tier meters 8,000
# TOKENS PER MINUTE, and max_tokens is charged against the budget up front - before a single
# token is generated. At 8,000 the very first request was rejected 413 on its own
# ("Limit 8000, Requested 8819") with the prompt barely started. 2,500 leaves ~5,500 per minute
# for the system prompt, the tool schema and the accumulated conversation, and a brief runs
# ~800-1,200 tokens, so it is not a real constraint on the output.
MAX_TOKENS = 2500

# --- The production tool-call ceiling ---------------------------------------------------
#
# Day 7's harness used 5. That was a number chosen to make the cap fire inside a test, not an
# estimate of what an investigation needs, and reusing it would abort every real run.
#
# Derived instead from the evidence a brief has to cite, one query each unless noted:
#
#   1  the incident's own per-day shape          detected_anomaly_points for the anomaly_key
#   2  how wide the slice is                     other anomalies in the same window
#   3  revenue either side of the window         the event needs a before and an after
#   4  units vs revenue                          separates a discount-driven lift from demand
#   5  marketing spend, same window              the first-pass correlation
#   6  marketing spend, extended lead window     spend moves BEFORE revenue - 2 days on the
#                                                promotion, 5 on the budget cut. A same-day
#                                                query alone reads as "no relationship"
#   7  inventory / stockout                      the inventory hypothesis
#   8  margin                                    separates a price event from a volume event
#   9  peer cells outside the slice              specificity: did only this slice move?
#  10  one follow-up on whichever of 5-9 fired
#
# Ten productive calls. Models do not walk a checklist cleanly - they re-query after an empty
# result, narrow a window, or fix a rejected query - so the observed-to-minimum ratio is
# comfortably above one. Doubling gives 20, which is the ceiling set here. A smaller model
# rewrites rejected queries more often, which spends budget without gathering evidence, so the
# eval reports rejected calls separately from productive ones.
#
# 20 is a hypothesis, not a measurement. docs/day8_agent_eval.md reports the tool calls each
# eval investigation actually used, and whether any hit the ceiling.
MAX_TOOL_CALLS = 20

# Where briefs land. One markdown file per investigation, kept in git, because the brief is the
# deliverable this whole project exists to produce - a log line is not an artifact.
BRIEFS_DIR = "docs/sample_briefs"


def build_http_client():
    """Shared TLS-tolerant httpx client, or None on a machine without the interception problem.

    AVG's root omits the `critical` flag on its basicConstraints extension, and Python 3.13+
    turns on OpenSSL's VERIFY_X509_STRICT by default, which rejects that as malformed - the same
    'Basic Constraints of CA cert not marked critical' error that blocked the dbt 1.12 install on
    Day 3. Clearing that one flag tolerates the non-conformant encoding while leaving chain
    verification and hostname checking fully on. `verify=False` would be the unacceptable
    version of this fix; the connection carries an API key.
    """
    if not os.path.exists(_CA_BUNDLE):
        return None

    import ssl

    import httpx

    context = ssl.create_default_context(cafile=_CA_BUNDLE)
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return httpx.Client(verify=context, timeout=600.0)


def build_provider():
    """The one call sites use. Returns an LLMProvider; nothing outside agent/llm/ imports a
    vendor SDK."""
    from .llm import build_provider as _build

    return _build(cfg=__import__(__name__, fromlist=["_"]), http_client=build_http_client())
