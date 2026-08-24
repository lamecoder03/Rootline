# Which LLM, how many turns, how big a brief - separate from the guardrail boundary in
# agent/guardrails/config.py, which this file must never reach into.
# Exists so a provider switch is a value change here plus an adapter under agent/llm/, and the
# investigation logic, the tool, and every Day 7 guardrail stay untouched.

import os

from dotenv import load_dotenv

# The dbt `.env` gotcha, now applying to API keys as well as the database: `.env` is a file,
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
# still present and working; the switch is a cost decision, documented in the README's
# provider-pivot note rather than quietly applied. `agent/llm/` is what makes it a one-line change.
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
# ~800-1,200 tokens, so it is not a real constraint on the output. Lowered again to 1,200
# once the DAILY quota surfaced: max_tokens is billed up front against a 200,000/day budget
# too, so every unused token of headroom is paid for on every one of the twenty calls.
MAX_TOKENS = 1200

# The per-request ceiling, read from the live response headers rather than from documentation:
# x-ratelimit-limit-tokens = 8000 on every tool-calling model this account can reach. Groq bills
# prompt + max_tokens against it TOGETHER and up front, so the real constraint is
#
#     fixed overhead + conversation so far + max_tokens  <=  8000
#
# Measured fixed overhead is 2,042 tokens (system prompt 5,396 chars + tool schema 2,208 +
# the anomaly row 548, at 3.99 chars/token). That leaves roughly 4,000 tokens for the entire
# evidence trail of a twenty-call investigation, which is not enough to keep every result in
# full - hence agent/context_budget.py, which elides oldest-first and says so.
#
# This is a free-tier limit, not a property of the model: gpt-oss-120b's own context window is
# far larger. groq/compound and compound-mini carry 70,000 TPM but return
# "`tool calling` is not supported with this model", so the larger budget is unreachable here.
CONTEXT_TOKEN_LIMIT = int(os.getenv("LLM_CONTEXT_TOKEN_LIMIT", "8000"))

# Never ask for a brief shorter than this. If the history cannot be squeezed far enough to leave
# this much room, the investigation has a problem that a smaller answer will not fix.
MIN_OUTPUT_TOKENS = 700

# The same 8,000 figure, used for a different job: pacing. One investigation request costs most
# of a minute's quota, so requests must be spaced rather than merely retried. Reacting to 429s
# was measured failing outright - four consecutive 65-second waits made no progress, because a
# retry that under-sleeps is refused again and the window never clears. agent/llm/pacing.py
# waits for the quota BEFORE spending it, which is possible only because Groq bills
# prompt + max_tokens up front and both are known before the request is sent.
TOKENS_PER_MINUTE = CONTEXT_TOKEN_LIMIT

# gpt-oss models think before they answer, and those reasoning tokens are billed as output and
# charged against max_tokens. Measured: a turn on gpt-oss-20b consumed its entire 1,200-token
# allowance on reasoning and returned an EMPTY brief with finish_reason=length. "medium" keeps
# enough deliberation for a lead-lag argument while leaving room to actually write the answer.
REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "medium")

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
# Ten productive calls, several of which combine (revenue, units and spend come back from one
# query, not three). The first ceiling set here was 20 - that ten, doubled, on the reasoning that
# models re-query after an empty result and the observed-to-minimum ratio must exceed one.
#
# THAT WAS A HYPOTHESIS, AND THE MEASUREMENT CONTRADICTED IT. Lowered to 8 on the evidence below.
#
# What was measured (DET-0008, gpt-oss-20b, ceiling 20, investigation INV-DET-0008-1f183d):
# all 20 calls were spent, and calls 13-20 re-queried tables already read at calls 1-9 -
# detected_anomaly_points four separate times, marketing spend for the same March 7-21 window
# three times. The investigation then produced NO brief at all.
#
# The mechanism is not impatience, it is the context window. A request must fit
# fixed overhead + conversation + max_tokens <= 8,000 tokens, so agent/context_budget.py elides
# the oldest results once roughly the eighth accumulates. Past that point every new call pushes
# an earlier result out of view, the model notices a figure it needs is gone, and spends the next
# call re-fetching it - which evicts another. The marginal call beyond ~8 destroys more evidence
# than it adds, and the loop is self-sustaining.
#
# So 8 is where the evidence budget and the context budget agree: it is both what the ten-item
# checklist actually needs once queries are combined, and the most results that can stay
# simultaneously visible. A ceiling above the window is not a bigger allowance, it is a churn
# generator.
#
# Cost is a consequence of this, not the reason for it. Because every call re-sends the whole
# conversation, spend is quadratic in calls: 20 calls measured ~152,000 tokens, 8 costs ~40,000.
# That the change also makes the eval affordable on a free tier is a welcome side effect of
# fixing a reasoning defect, and would not on its own justify degrading the agent.
#
# The eval harness records per investigation - calls used, calls rejected, and how many result
# sets had to be elided - so this number stays a measurement, not a belief. No scored run has
# completed yet, so no results file exists; agent/eval/run_eval.py writes one when it does.
MAX_TOOL_CALLS = 8

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
