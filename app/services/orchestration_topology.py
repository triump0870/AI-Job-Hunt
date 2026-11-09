"""The AI Orchestration topology: every stage of the Job Hunt OS pipeline, what kind of work it is, and how work
moves between stages.

This is the registry the AI Orchestration workspace draws (app.services.orchestration, app/static/orchestration.js).
It is *topology*, not runtime: it changes when the system gains a stage, never when a job moves. Runtime state —
queues, packets, metrics — is computed from the business and telemetry tables by app.services.orchestration.

Adding a stage
--------------
1. Give it a stable lower-case `id`. It is an API identifier: never rename one.
2. Say what kind of work it is (`types`, primary first) — be honest: a deterministic step is not an AI node.
3. Name the AI workers it sends (`workers`, keys of app.services.ai_workers.WORKERS) and the Monitoring stages
   its LLM requests are recorded under (`llm_stages`, app.models.LLM_STAGES). Telemetry is matched by these.
4. Name the third-party calls it makes (`external`: provider, and an operation prefix when the provider is shared).
5. Add the edges it takes part in. A count on an edge must come from a stored fact (`measure` says which).
6. Leave `implemented=False` for a stage that is designed but not built yet: it is listed, never drawn as live.

Nothing here reads the database, and the frontend never hard-codes a stage: it renders whatever this returns.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.models import LLM_STAGES
from app.services.ai_workers import WORKERS

# What kind of work a node does. A node may be several (JD recovery is HTTP, a browser and an optional model).
NODE_TYPES: dict[str, dict[str, str]] = {
    "AI": {"label": "AI worker", "symbol": "●", "description": "Sends prompts to a model through the LLM gateway"},
    "DETERMINISTIC": {"label": "Deterministic", "symbol": "◆", "description": "Plain code: same input, same result"},
    "EXTERNAL": {"label": "External service", "symbol": "■", "description": "Calls a third-party API"},
    "HUMAN_GATE": {"label": "Human / policy gate", "symbol": "◇", "description": "Waits for a person or a policy"},
    "BROWSER": {"label": "Browser automation", "symbol": "▣", "description": "Uses the shared headless browser"},
    "ASYNC_WORKER": {"label": "Async worker", "symbol": "↻", "description": "Queued; the scheduler picks it up"},
    "OUTCOME": {"label": "Outcome", "symbol": "○", "description": "Where a job ends up"},
}
# Lanes group nodes on the canvas; `position` is (column, row) on a grid the frontend scales. Row -1 holds the
# supporting workers, row 0 runs left to right (intake), row 1 right to left (the packet), row 2 the outcomes.
LANES = ("pipeline", "outcome", "support")


@dataclass(frozen=True)
class ExternalMatch:
    """Which external_api_request_logs rows belong to a node."""

    provider: str
    operation_prefix: str | None = None  # for a shared provider (WEB): only these operations


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    types: tuple[str, ...]  # primary first
    description: str
    lane: str
    position: tuple[int, int]
    workers: tuple[str, ...] = ()
    llm_stages: tuple[str, ...] = ()
    external: tuple[ExternalMatch, ...] = ()
    # The model is a fallback, not the main mechanism (JD recovery asks a model only on an ambiguous page).
    optional_ai: bool = False
    # Canonical workflow states (app.services.lifecycle) a job is in while it is at this node.
    states: tuple[str, ...] = ()
    # Where to look deeper: a Monitoring tab and a workspace screen.
    monitoring_tab: str | None = None
    screen: str | None = None
    # Its queue in Running workers (app.services.workers kind), when it has one.
    workers_tab: str | None = None
    implemented: bool = True

    @property
    def primary_type(self) -> str:
        return self.types[0]

    @property
    def is_ai(self) -> bool:
        return "AI" in self.types

    def public(self) -> dict[str, Any]:
        out = {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self).items()}
        out["external"] = [asdict(e) for e in self.external]
        out["position"] = {"col": self.position[0], "row": self.position[1]}
        out["primary_type"] = self.primary_type
        return out


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    label: str
    kind: str = "flow"  # flow | branch | support
    # What a count on this edge means, in words, and which stored fact it comes from. None = topology only.
    measure: str | None = None
    # Whether a per-item time between the two stages is recorded (both timestamps stored on the same row).
    timed: bool = False

    @property
    def id(self) -> str:
        return f"{self.source}__{self.target}"

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "source": self.source, "target": self.target, "label": self.label, "kind": self.kind,
                "measure": self.measure, "timed": self.timed}


AI, DET, EXT, HUMAN, BROWSER, ASYNC, OUTCOME = "AI", "DETERMINISTIC", "EXTERNAL", "HUMAN_GATE", "BROWSER", \
    "ASYNC_WORKER", "OUTCOME"

NODES: tuple[Node, ...] = (
    # ── intake: row 0, left to right ──
    Node("discovery", "Discovery", (EXT, DET), "Hunter and saved searches import postings from Apify and job boards; "
         "imports are normalized and deduplicated in code.", "pipeline", (0, 0),
         external=(ExternalMatch("APIFY"), ExternalMatch("JOB_BOARD")), states=("DISCOVERED",),
         monitoring_tab="apify", screen="discovery"),
    Node("domain_filter", "Domain filter", (DET,), "Deterministic domain match (app.services.domain_match): hides only "
         "what its rules can settle, before any model sees it. No model is called here.", "pipeline", (1, 0),
         states=("NORMALIZED",), screen="discovery"),
    Node("domain_classifier", "Domain check", (AI, ASYNC), "A fast model reads jobs the rules could not settle "
         "(app.services.domain_classifier, DOMAIN_CLASSIFIER_MODE). MATCH and UNCERTAIN go on; code keeps a NO_MATCH only "
         "when its evidence holds, and a failed check never hides a job.", "pipeline", (2, 0),
         workers=("DOMAIN_CLASSIFIER",), llm_stages=("DOMAIN_CLASSIFIER",), optional_ai=True, monitoring_tab="llm",
         workers_tab="classification", screen="discovery"),
    Node("resume_match", "Resume match", (DET,), "Chooses which of your resumes a job is ranked and tailored "
         "against: the saved search's pin, else tag matching (app.services.resume_matching).", "pipeline", (3, 0),
         screen="discovery"),
    Node("jd_recovery", "JD recovery", (ASYNC, EXT, BROWSER, AI), "Reads a posting's description from its URL when "
         "the board sent none: HTTP first, the shared browser second, a model only to pick blocks on an ambiguous "
         "page.", "pipeline", (4, 0), workers=("JD_RECOVERY",), llm_stages=("JD_RECOVERY",),
         external=(ExternalMatch("WEB", "jd_recovery"),), optional_ai=True, monitoring_tab="external", workers_tab="recovery",
         screen="discovery"),
    Node("jd_parser", "JD parser", (AI, ASYNC), "Parses the posting into structured requirements with evidence "
         "sentences.", "pipeline", (5, 0), workers=("JD_PARSER",), llm_stages=("JD_PARSER",),
         monitoring_tab="llm", workers_tab="parsing", screen="discovery"),
    Node("ranker", "Ranker", (AI, ASYNC), "Scores fit against verified evidence; Python, not the model, decides "
         "QUALIFIED or ARCHIVED.", "pipeline", (6, 0), workers=("RANKER",), llm_stages=("RANKER",),
         states=("FILTERED", "RANK_PENDING", "RANKING", "RANKED"), monitoring_tab="llm", screen="discovery",
         workers_tab="ranking"),
    # ── packet: row 1, right to left ──
    Node("tailor", "Tailor", (AI, ASYNC), "Presents verified experience for the job as a resume patch, and writes "
         "the cover letter. Never scores or fills gaps.", "pipeline", (5, 1), workers=("TAILOR",),
         llm_stages=("TAILOR",), states=("QUALIFIED", "TAILORING", "TAILORED"), monitoring_tab="llm", screen="tailor",
         workers_tab="tailoring"),
    Node("reviewer", "Reviewer", (AI,), "Independently checks the tailored packet and recommends PASS, RETAILOR or "
         "SKIP; code decides.", "pipeline", (4, 1), workers=("REVIEWER",), llm_stages=("REVIEWER",),
         states=("REVIEWED",), monitoring_tab="llm", screen="tailor", workers_tab="reviewing"),
    Node("approval", "Approval", (HUMAN,), "Nothing is submitted without your sign-off; the reviewer minimum is a "
         "policy you can override with a reason.", "pipeline", (3, 1), states=("READY_TO_APPLY",),
         screen="approvals"),
    Node("application", "Application", (HUMAN, AI), "Submission: you (or Claude Desktop over MCP) fill the form; the "
         "Application Answer worker drafts answers to form questions (on Job Insights, from verified evidence; personal "
         "questions are never drafted).", "pipeline", (2, 1),
         workers=("APPLICATION_ANSWER",), llm_stages=("APPLICATION",), states=("APPLYING", "APPLIED"),
         optional_ai=True, screen="approvals"),
    Node("application_inbox", "Application inbox", (ASYNC, EXT, AI), "Reads Gmail (read-only) for replies to "
         "submitted applications; a model classifies matched emails, code decides any stage change.", "pipeline",
         (1, 1), workers=("INBOX_CLASSIFIER",), llm_stages=("INBOX",),
         external=(ExternalMatch("GMAIL"), ExternalMatch("GOOGLE_OAUTH")), monitoring_tab="inbox",
         screen="tracker"),
    Node("follow_up", "Follow-up", (AI, HUMAN), "Drafts a follow-up when one is due; a recruiter reply cancels it. "
         "You send it.", "pipeline", (0, 1), workers=("FOLLOWUP",), llm_stages=("FOLLOW_UP",),
         states=("FOLLOWUP_DUE", "FOLLOWED_UP"), monitoring_tab="llm", screen="tracker"),
    # ── outcomes ──
    Node("archived", "Archived", (OUTCOME,), "Archived by the ranking policy (below your fit floor or a fatal gap).",
         "outcome", (7, 0), states=("ARCHIVED",), screen="discovery"),
    Node("interview", "Interview", (HUMAN,), "Screening, interviews and take-homes, as the tracker records them.",
         "outcome", (1, 2), states=("INTERVIEW",), screen="tracker"),
    Node("outcome", "Offer / rejected", (OUTCOME,), "Where an application ended: offer, rejection or closed.",
         "outcome", (2, 2), states=("OFFER", "REJECTED"), screen="tracker"),
    # ── supporting workers ──
    Node("resume_parser", "Resume parser", (AI, ASYNC), "Structures each uploaded resume with the Resume Parser model "
         "(LangGraph), keeps only what the resume's own text states, and extracts verifiable evidence the Ranker and "
         "Tailor may cite.", "support", (5, -1), workers=("RESUME_PARSER", "EVIDENCE_EXTRACTION"),
         llm_stages=("RESUME_PARSER",), monitoring_tab="llm", screen="settings", workers_tab="resume_parsing"),
    Node("compensation", "Compensation", (DET,), "Reads salary from structured posting fields only; the "
         "description is never mined.", "support", (0, -1), screen="discovery"),
    Node("ai_advisor", "AI Optimization Advisor", (AI, ASYNC), "Analyses LLM telemetry and recommends; it never "
         "changes a model, a route or a setting.", "support", (6, -1), workers=("AI_OPTIMIZATION",),
         llm_stages=("ADVISOR",), monitoring_tab="advisor"),
    # Job Insights (app.services.job_insights): researched in the background once a job is approved, so the nodes sit
    # right under Approval.
    Node("company_insights", "Job insights research", (ASYNC, EXT, AI), "Researches an approved job: the posting, "
         "the company's own site, and Glassdoor and PayScale through Apify when an admin configured them; a model "
         "then organises what was found, citing every source. Salary is computed in code.", "support", (3, 2),
         workers=("COMPANY_INSIGHTS",), llm_stages=("COMPANY_INSIGHTS",),
         # The operations job insights actually emits (app.services.job_insights.apify / sources).
         external=(ExternalMatch("APIFY", "glassdoor_"), ExternalMatch("APIFY", "payscale_"),
                   ExternalMatch("WEB", "official_company_page")),
         monitoring_tab="external", workers_tab="insights", screen="approvals"),
    # The retrieval indexer (app.services.rag_index): embeds verified evidence and saved answers. An embedding model
    # makes vectors, not text, and sends no prompt, so this is not an AI node; the call is an external one.
    Node("retrieval_index", "Retrieval index", (ASYNC, EXT), "Embeds your verified evidence and saved answers through "
         "the LiteLLM embeddings endpoint, so retrieval can find them by meaning. Off unless RAG_VECTOR_ENABLED; it "
         "never generates text, and what a job may use is still decided in code.", "support", (4, -1),
         external=(ExternalMatch("LITELLM", "embed_documents"),), monitoring_tab="external", workers_tab="indexing",
         screen="knowledge"),
    # The qualification graph (app.agents.qualification, QUALIFICATION_GRAPH_MODE): a conductor over the intake row.
    # It admits a job first, then hands it to recovery, parsing and ranking through their own task rows and waits;
    # it never calls a model, so it is not an AI node. A job it waits on is shown at the worker doing the step.
    Node("qualification", "Qualification graph", (ASYNC, DET), "Takes each new job through admission, description "
         "recovery, parsing, hard filters, evidence scope and ranking in that order (LangGraph), through the same "
         "workers; a job outside your domain stops at admission with nothing spent. Off unless "
         "QUALIFICATION_GRAPH_MODE is on (or shadow, which only compares).", "support", (2, -1),
         workers_tab="qualification", screen="discovery"),
    # The resume preparation graph (app.agents.preparation, PREPARATION_GRAPH_MODE): a conductor over tailoring and
    # review. It hands each pass to the tailor worker and waits, checks the saved version in code, lets review_results
    # decide, loops a bounded number of times and waits for your approval; it never calls a model, so it is not an AI
    # node. A job it waits on is shown at the worker doing the step.
    Node("preparation", "Preparation graph", (ASYNC, DET), "Takes each qualified job through resume selection, the "
         "Ranker's analysis, evidence, tailoring, evidence checks and the review decision (LangGraph), with a bounded "
         "revision loop, then waits for your approval. A packet with an unsupported claim never passes. Off unless "
         "PREPARATION_GRAPH_MODE is on (or shadow, which only compares).", "support", (3, -1),
         workers_tab="preparation", screen="tailor"),
    # The application worker (app.agents.application, APPLICATION_EXECUTION_MODE): reads an approved job's form,
    # resolves each answer's source in code and, in assist mode, fills it — on Playwright with a form adapter, or on
    # the browser-use sidecar, where the Browser Navigator proposes steps Python checks (optional AI). In guarded
    # mode it may click the final Submit once, behind the pre-submit validator, the shared claim and an intent.
    Node("application_execution", "Application worker", (ASYNC, BROWSER, EXT, DET, AI), "Reads an approved job's "
         "application form, finds a verified source for every answer (unknown required answers wait for you) and, "
         "in assist mode, fills it in a private browser that cannot submit, ending at ready to submit. A form no "
         "adapter knows goes to the browser-use backend, where the Browser Navigator proposes each step and code "
         "checks it. In guarded mode (consenting accounts, allowlisted routes) it may submit once, after the "
         "pre-submit validator and the shared claim, and records it only on the site's confirmation. Off unless "
         "APPLICATION_EXECUTION_MODE is observe, assist or guarded_auto.", "support", (1, -1),
         workers=("BROWSER_NAVIGATOR",), llm_stages=("BROWSER_NAVIGATION",), optional_ai=True,
         external=(ExternalMatch("WEB", "application_browser"), ExternalMatch("BROWSER_USE"),
                   ExternalMatch("BROWSER_USE_WEBUI")), monitoring_tab="external",
         workers_tab="application", screen="approvals"),
    # Guarded submission (app.services.submission): the one click is bound to a SubmissionIntent, and only the
    # ConfirmationVerifier's evidence from the site makes it CONFIRMED; anything less is UNCERTAIN until a person says.
    Node("submission_confirmation", "Submission confirmation", (DET, OUTCOME), "After the application worker's one "
         "authorized Submit click, the site's own evidence (a successful request to the site, the form gone, its "
         "confirmation) decides CONFIRMED; anything less is UNCERTAIN and waits for you. Never retried, never "
         "guessed.", "support", (1, -2), workers_tab="application", screen="approvals"),
    # Email the matcher could not place with confidence (application_inbox.matching): linked to no application until
    # a person decides. A person's correction always wins over the classifier.
    Node("inbox_review", "Email review", (HUMAN,), "Emails the inbox could not match to one application with "
         "confidence, and classifications it was unsure of, wait here for you; your correction overrides the "
         "model's.", "outcome", (0, 2), monitoring_tab="inbox", screen="tracker"),
    # Planned (docs/architecture/agentic-roadmap.md Phase 7 follow-ups): drawn as planned, never measured.
    Node("ats_correction_memory", "ATS answer memory", (DET, HUMAN), "Planned: keep the corrections you approve on "
         "application forms as verified answers, without promoting generated text into facts. Not built yet.",
         "support", (2, -2), implemented=False),
    Node("eval_feedback", "Evaluation feedback", (DET,), "Planned: feed offline evaluation results and your "
         "corrections back to worker configuration as recommendations. Today evaluation runs offline "
         "(scripts/agent_eval.py) and changes nothing.", "support", (6, 1), implemented=False),
    Node("interview_insights", "Interview preparation", (AI,), "Writes interview preparation from the posting, the "
         "fit analysis and verified evidence, always labelled as generated (never as questions the company asks).",
         "support", (4, 2), workers=("INTERVIEW_INSIGHTS",), llm_stages=("INTERVIEW_INSIGHTS",),
         monitoring_tab="llm", screen="approvals"),
)

EDGES: tuple[Edge, ...] = (
    Edge("discovery", "domain_filter", "imported", measure="jobs created (jobs.discovered_at)", timed=True),
    Edge("domain_filter", "domain_classifier", "unsettled", measure="tasks queued for the model "
         "(job_classification_tasks route ASK)"),
    Edge("domain_classifier", "resume_match", "match or uncertain", measure="tasks that let the job through "
         "(job_classification_tasks classification MATCH or UNCERTAIN)"),
    Edge("domain_filter", "resume_match", "kept", measure="jobs the filter kept (jobs.domain_checked_at, "
         "domain_match MATCH or KEPT)"),
    Edge("resume_match", "jd_recovery", "no description", measure="recovery tasks created "
         "(job_description_fetch_tasks.created_at)", timed=True),
    Edge("resume_match", "jd_parser", "description present", measure="parse tasks created for jobs that never "
         "needed recovery (job_description_parse_tasks.created_at)"),
    Edge("jd_recovery", "jd_parser", "recovered", measure="recovery tasks that saved a description "
         "(result RECOVERED)"),
    Edge("jd_parser", "ranker", "parsed", measure="parse tasks completed (job_description_parse_tasks.completed_at)"),
    Edge("ranker", "tailor", "qualified", kind="branch", measure="ranking decisions QUALIFIED "
         "(workflow_transitions)"),
    Edge("ranker", "archived", "archived", kind="branch", measure="ranking decisions ARCHIVED "
         "(workflow_transitions)"),
    Edge("tailor", "reviewer", "tailored", measure="tailored versions saved (resume_variants.created_at)"),
    Edge("reviewer", "tailor", "re-tailor", kind="branch", measure="reviews that sent the packet back "
         "(resume_variants.reviewer_notes.decision RETAILOR)"),
    Edge("reviewer", "approval", "reviewed", measure="reviewed versions (resume_variants with a reviewer score)"),
    Edge("approval", "application", "approved", measure="approvals (applications.approved_at)", timed=True),
    Edge("application", "application_inbox", "submitted", measure="submissions (applications.applied_at)"),
    Edge("application_inbox", "follow_up", "follow-up due", kind="branch", measure="follow-ups created "
         "(followups.created_at)"),
    Edge("application_inbox", "interview", "interviewing", kind="branch", measure="tracker moves to an interview "
         "stage (application_events kind=stage)"),
    Edge("application_inbox", "outcome", "rejected / closed", kind="branch", measure="tracker moves to rejected or "
         "closed (application_events kind=stage)"),
    Edge("interview", "outcome", "offer", measure="tracker moves to offer or negotiating (application_events "
         "kind=stage)"),
    Edge("resume_parser", "ranker", "verified evidence", kind="support"),
    Edge("resume_parser", "retrieval_index", "verified evidence", kind="support"),
    Edge("retrieval_index", "ranker", "retrieval", kind="support"),
    Edge("compensation", "discovery", "salary enrichment", kind="support"),
    Edge("qualification", "domain_filter", "admits first", kind="support"),
    Edge("qualification", "ranker", "hands to ranking", kind="support"),
    Edge("preparation", "tailor", "hands to tailoring", kind="support"),
    Edge("preparation", "reviewer", "decides the review", kind="support"),
    Edge("preparation", "approval", "waits for you", kind="support"),
    Edge("approval", "company_insights", "research", kind="support", measure="insight tasks created "
         "(job_insight_tasks.created_at)", timed=True),
    Edge("approval", "application_execution", "form prepared", kind="support", measure="application runs queued "
         "(application_execution_tasks.created_at)", timed=True),
    Edge("application_execution", "application", "ready to submit", kind="support", measure="application runs that "
         "ended READY_TO_SUBMIT (application_execution_tasks.completed_at)"),
    Edge("application_execution", "submission_confirmation", "submit clicked", kind="support",
         measure="intents committed to the click (submission_intents.clicking_at)", timed=True),
    Edge("submission_confirmation", "application", "confirmed", kind="support", measure="submissions the site "
         "confirmed (submission_intents CONFIRMED, resolved_at)"),
    Edge("application_inbox", "inbox_review", "needs review", kind="branch", measure="emails waiting for a person "
         "(application_email_messages NEEDS_REVIEW, and classifications marked needs_review)"),
    Edge("company_insights", "interview_insights", "researched", kind="support", measure="research runs that saved "
         "insights (job_insight_tasks COMPLETED or PARTIAL)"),
)

NODES_BY_ID: dict[str, Node] = {n.id: n for n in NODES}
EDGES_BY_ID: dict[str, Edge] = {e.id: e for e in EDGES}


def node(node_id: str) -> Node | None:
    return NODES_BY_ID.get(node_id)


def edge(source: str, target: str) -> Edge | None:
    return EDGES_BY_ID.get(f"{source}__{target}")


def node_for_worker(worker_type: str | None) -> Node | None:
    return next((n for n in NODES if worker_type and worker_type in n.workers), None)


def node_for_llm_stage(stage: str | None) -> Node | None:
    return next((n for n in NODES if n.implemented and stage and stage in n.llm_stages), None)


def node_for_state(state: str | None) -> Node | None:
    return next((n for n in NODES if state and state in n.states), None)


def validate() -> list[str]:
    """Problems with the registry itself; tests keep this empty."""
    problems = []
    for n in NODES:
        if not n.types or any(t not in NODE_TYPES for t in n.types):
            problems.append(f"{n.id}: unknown node type in {n.types}")
        if n.lane not in LANES:
            problems.append(f"{n.id}: unknown lane {n.lane}")
        problems += [f"{n.id}: unknown worker {w}" for w in n.workers if w not in WORKERS]
        problems += [f"{n.id}: unknown LLM stage {s}" for s in n.llm_stages if s not in LLM_STAGES]
        if n.is_ai and not n.workers:
            problems.append(f"{n.id}: an AI node must name its workers")
    if len(NODES_BY_ID) != len(NODES):
        problems.append("duplicate node id")
    positions = [(n.position, n.id) for n in NODES]
    if len({p for p, _ in positions}) != len(positions):
        problems.append("two nodes share a grid position")
    for e in EDGES:
        for end in (e.source, e.target):
            if end not in NODES_BY_ID:
                problems.append(f"edge {e.id}: unknown node {end}")
    return problems


def public() -> dict[str, Any]:
    """The whole topology as the API returns it: node types, lanes, nodes and edges. Static, safe to cache.

    Planned stages are listed separately (`reserved`, with their grid position) and are never measured or given a
    status: the canvas draws them as planned, so a stage that does not run never looks as if it does."""
    nodes = [n for n in NODES if n.implemented]
    ids = {n.id for n in nodes}
    return {"node_types": NODE_TYPES, "lanes": list(LANES),
            "nodes": [n.public() for n in nodes],
            "edges": [e.public() for e in EDGES if e.source in ids and e.target in ids],
            "reserved": [{"id": n.id, "label": n.label, "description": n.description, "lane": n.lane,
                          "position": list(n.position), "planned": True} for n in NODES if not n.implemented]}


__all__ = ["NODES", "EDGES", "NODE_TYPES", "Node", "Edge", "ExternalMatch", "node", "edge", "node_for_worker",
           "node_for_llm_stage", "node_for_state", "validate", "public"]
