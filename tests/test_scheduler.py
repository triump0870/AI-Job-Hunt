"""The scheduler engine: start/stop, duplicate runs, leases across instances, failure isolation, restarts.

Engine tests run on a throwaway SQLite database so they always run; the multi-instance race is repeated
on PostgreSQL when TEST_DATABASE_URL is set. The clock is injected, so nothing waits for real intervals.
"""
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db_migrations import run_migrations
from app.models import SchedulerJob
from app.scheduler import Scheduler, build_scheduler
from app.services.scheduled_jobs import JOBS, JobSpec

T0 = datetime(2026, 5, 1, 9, 0, 0)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}", connect_args={"check_same_thread": False, "timeout": 15})
    run_migrations(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def spec(name, run):
    return JobSpec(name, name.title(), f"{name}_interval_minutes", run, "test job")


def make(sessions, specs, clock, instance="instance-a", minutes=5):
    return Scheduler(specs, sessions, intervals={s.name: minutes for s in specs}, tick_seconds=0.02,
                     lease_seconds=600, instance_id=instance, clock=clock)


def row(sessions, name) -> SchedulerJob:
    with sessions() as db:
        return db.get(SchedulerJob, name)


def test_app_scheduler_has_every_job_configured_from_settings():
    scheduler = build_scheduler(enabled=False)
    assert list(scheduler.specs) == ["hunter", "apify_sync", "ranking", "domain_classifier", "jd_parsing", "resume_parse", "jd_recovery",
                                     "listing_check",
                                     "compensation", "job_insights", "auto_tailor",
                                     "applications", "followups", "inbox", "analytics", "ai_optimization",
                                     "litellm_telemetry", "model_health", "agent_graphs", "qualification", "preparation",
                                     "application_execution", "rag_index",
                                     "cleanup"]
    assert scheduler.interval("agent_graphs") == 0  # off unless AGENT_GRAPHS_ENABLED
    assert scheduler.interval("application_execution") == 0  # off unless AGENT_GRAPHS_ENABLED
    assert scheduler.interval("rag_index") == 0  # off unless RAG_VECTOR_ENABLED
    assert scheduler.interval("jd_recovery") == 2
    assert scheduler.interval("ranking") == 2 and scheduler.interval("hunter") == 240
    assert scheduler.interval("ai_optimization") == 5 and scheduler.interval("auto_tailor") == 5
    assert {spec.name for spec in JOBS} == set(scheduler.specs)


def test_scheduler_starts_runs_due_jobs_and_stops(sessions):
    calls = []
    clock = Clock()
    scheduler = make(sessions, [spec("ping", lambda ctx: calls.append(ctx.trigger) or {"ok": True})], clock)

    async def run_for(seconds):
        await scheduler.start()
        running = scheduler.running
        await asyncio.sleep(seconds)
        await scheduler.stop()
        return running

    assert not scheduler.running
    assert asyncio.run(run_for(0.2)) is True  # many ticks, but the job is not due again until the clock moves
    assert not scheduler.running and calls == ["schedule"]
    job = row(sessions, "ping")
    assert (job.last_status, job.run_count, job.locked_by) == ("succeeded", 1, None)
    assert job.next_run_at == T0 + timedelta(minutes=5) and job.last_result == {"ok": True}

    clock.advance(300)
    assert asyncio.run(run_for(0.1)) is True  # a stopped scheduler starts again cleanly
    assert calls == ["schedule", "schedule"]


def test_duplicate_runs_are_safe(sessions):
    entered, release, calls = threading.Event(), threading.Event(), []

    def slow(ctx):
        calls.append(ctx.trigger)
        entered.set()
        release.wait(5)
        return {"done": True}

    scheduler = make(sessions, [spec("slow", slow)], Clock())

    async def scenario():
        first = asyncio.create_task(scheduler.run_job("slow", "schedule"))
        await asyncio.to_thread(entered.wait, 5)
        while_running = [await scheduler.run_job("slow", "manual"), await scheduler.run_job("slow", "schedule")]
        release.set()
        done = await first
        return while_running, done, await scheduler.run_job("slow", "schedule")

    while_running, done, again = asyncio.run(scenario())
    assert [o.status for o in while_running] == ["busy", "busy"]
    assert "instance-a" in while_running[0].error
    assert done.status == "succeeded" and again.status == "not_due"
    assert calls == ["schedule"] and row(sessions, "slow").run_count == 1


def test_two_instances_never_run_the_same_job_together(sessions):
    calls, lock = [], threading.Lock()

    def work(ctx):
        with lock:
            calls.append(ctx.worker_id)
        time.sleep(0.2)
        return {}

    clock = Clock()
    first = make(sessions, [spec("work", work)], clock, instance="instance-a")
    second = make(sessions, [spec("work", work)], clock, instance="instance-b")

    async def scenario():
        return await asyncio.gather(first.run_job("work", "schedule"), second.run_job("work", "schedule"))

    outcomes = asyncio.run(scenario())
    assert sorted(o.status for o in outcomes) == ["busy", "succeeded"]
    assert len(calls) == 1


def test_two_instances_share_the_lease_on_postgresql(pg_sessions):
    calls, lock = [], threading.Lock()

    def work(ctx):
        with lock:
            calls.append(ctx.worker_id)
        time.sleep(0.3)
        return {}

    clock = Clock()
    instances = [make(pg_sessions, [spec("work", work)], clock, instance=f"instance-{n}") for n in range(4)]
    barrier = threading.Barrier(len(instances))

    def run(scheduler):
        barrier.wait()
        return asyncio.run(scheduler.run_job("work", "schedule")).status

    with ThreadPoolExecutor(len(instances)) as pool:
        statuses = list(pool.map(run, instances))
    assert statuses.count("succeeded") == 1 and statuses.count("busy") == 3
    assert len(calls) == 1


def test_a_failing_job_does_not_stop_the_other_jobs(sessions):
    calls = []

    def broken(ctx):
        raise RuntimeError("provider down")

    async def broken_async(ctx):
        raise ValueError("bad data")

    clock = Clock()
    scheduler = make(sessions, [spec("broken", broken), spec("broken_async", broken_async),
                                spec("healthy", lambda ctx: calls.append(1) or {})], clock)

    async def scenario():
        await scheduler.start()
        await asyncio.sleep(0.2)
        clock.advance(301)
        await asyncio.sleep(0.2)
        still_running = scheduler.running
        await scheduler.stop()
        return still_running

    assert asyncio.run(scenario()) is True
    assert len(calls) == 2
    job = row(sessions, "broken")
    assert (job.last_status, job.run_count, job.failure_count) == ("failed", 2, 2)
    assert job.last_error == "RuntimeError: provider down" and job.locked_by is None
    assert row(sessions, "broken_async").last_error == "ValueError: bad data"
    assert row(sessions, "healthy").failure_count == 0


def test_restart_takes_over_a_lease_left_by_a_crashed_instance(sessions):
    calls = []
    clock = Clock()
    specs = [spec("sync", lambda ctx: calls.append(ctx.worker_id) or {})]
    crashed = make(sessions, specs, clock, instance="crashed")
    assert crashed._claim("sync", "crashed:run", clock(), "schedule")[0]  # claimed, then the process died

    restarted = make(sessions, specs, clock, instance="restarted")
    status = {job["name"]: job for job in restarted.status()["jobs"]}["sync"]
    assert status["running"] is True and status["locked_by"] == "crashed:run"
    assert asyncio.run(restarted.run_job("sync", "schedule")).status == "busy"

    clock.advance(601)  # the crashed instance's lease expires
    assert {job["name"]: job for job in restarted.status()["jobs"]}["sync"]["last_status"] == "interrupted"
    outcome = asyncio.run(restarted.run_job("sync", "schedule"))
    assert outcome.status == "succeeded" and calls[0].startswith("restarted:")


def test_zero_interval_disables_scheduling_but_not_manual_runs(sessions):
    clock = Clock()
    scheduler = Scheduler([spec("on", lambda ctx: {"n": 1}), spec("off", lambda ctx: {"skipped": "nothing to do"})],
                          sessions, intervals={"on": 5, "off": 0}, tick_seconds=1, lease_seconds=600,
                          instance_id="i", clock=clock)

    async def tick_once():
        started = await scheduler.tick()
        await asyncio.gather(*scheduler._inflight.values())
        return started

    assert asyncio.run(tick_once()) == ["on"]
    jobs = {job["name"]: job for job in scheduler.status()["jobs"]}
    assert jobs["on"]["last_status"] == "succeeded" and jobs["on"]["last_result"] == {"n": 1}
    assert jobs["off"]["enabled"] is False and jobs["off"]["last_status"] is None
    manual = asyncio.run(scheduler.run_job("off", "manual"))
    assert manual.status == "skipped" and manual.result == {"skipped": "nothing to do"}


def test_a_nudge_runs_a_job_now_and_once_more_when_it_arrives_mid_run(sessions):
    from app import scheduler_hooks

    clock = Clock()
    started, release = [], threading.Event()

    def work(ctx):
        started.append(ctx.trigger)
        if len(started) == 1:
            release.wait(5)  # still running when the second nudge arrives
        return {"n": len(started)}

    scheduler = Scheduler([spec("ranking", work), spec("off", lambda ctx: {"n": 0})], sessions,
                          intervals={"ranking": 5, "off": 0}, tick_seconds=3600, lease_seconds=600, instance_id="i",
                          clock=clock)

    async def scenario():
        await scheduler.start()
        try:
            await asyncio.sleep(0.2)  # the start-up tick ran it once on schedule
            scheduler_hooks.nudge("ranking")  # during that first run
            scheduler_hooks.nudge("ranking")  # and again: one extra run is enough
            scheduler_hooks.nudge("off")  # interval 0 means disabled: never run by a nudge
            await asyncio.sleep(0.2)
            release.set()
            await asyncio.sleep(0.6)
            return list(started)
        finally:
            await scheduler.stop()

    assert asyncio.run(scenario()) == ["schedule", "manual"]
    assert scheduler_hooks._wakers == {}  # unregistered on stop
