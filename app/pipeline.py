"""Four-stage research pipeline (Planner -> Researcher -> Verifier -> Writer).

Every job owns an independent JobState and its own WebTools instance (and page cache),
so concurrent jobs can never see each other's notes or findings. There is no shared
module-level mutable state for findings.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from typing import Any, Awaitable, TypeVar
from urllib.parse import urlparse

from pydantic import ValidationError

from . import prompts
from .config import Settings
from .llm import BaseLLM, LLMError, LLMUnavailable, safe_json_parse, strip_think
from .schemas import Finding, RawFinding, SubQuestion, Verdict
from .storage import Storage
from .tools import SKIP_EXTENSIONS, WebTools

log = logging.getLogger("pazhuhesh_jostejoo")
T = TypeVar("T")

DEPTH_COUNTS = {"quick": 2, "standard": 4, "deep": 6}
MIN_VERIFIED = 3            # below this, "unchecked" findings may be used by the writer
MAX_WRITER_FINDINGS = 24
MAX_FINDINGS_PER_PAGE = 3
PAGES_PER_QUESTION = 2
PAGE_CHARS_FOR_LLM = 4000
WRITER_TIMEOUT = 120

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_FA_TO_LATIN = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def fa(value: Any) -> str:
    """Latin digits -> Persian digits."""
    return str(value).translate(_FA)


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


# =============================================================== job state

class StopJob(Exception):
    """Raised inside a stage when the user cancelled or the time budget ran out."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "cancelled" | "timeout"


class JobState:
    def __init__(self, topic: str, depth: str, timeout: int) -> None:
        self.id = uuid.uuid4().hex
        self.topic = topic
        self.depth = depth
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.deadline = time.monotonic() + timeout
        self.events: list[dict[str, Any]] = []
        self.cond = asyncio.Condition()
        self.cancel_event = asyncio.Event()
        self.finished = False
        self.status = "running"          # running | completed | failed | cancelled
        self.progress = 0
        self.stage = "planning"
        self.plan: list[SubQuestion] = []
        self.findings: list[Finding] = []
        self.report_md = ""
        self.sources: list[dict[str, Any]] = []
        self.partial = False
        self.stop_reason: str | None = None
        self.searches = 0
        self.search_network_errors = 0
        self.task: asyncio.Task | None = None

    # ---- events

    async def emit(self, type_: str, message: str, progress: int, stage: str, **data: Any) -> None:
        self.progress = max(self.progress, min(100, int(progress)))
        self.stage = stage
        event = {
            "type": type_, "message": message, "progress": self.progress,
            "stage": stage, "data": data, "ts": time.time(),
        }
        async with self.cond:
            event["seq"] = len(self.events)
            self.events.append(event)
            self.cond.notify_all()

    async def finish(self, status: str | None = None) -> None:
        if status:
            self.status = status
        async with self.cond:
            self.finished = True
            self.finished_at = time.time()
            self.cond.notify_all()

    def request_cancel(self) -> None:
        self.cancel_event.set()

    # ---- cooperative stop / deadline

    async def guard(self, coro: Awaitable[T]) -> T:
        """Run `coro`, but abort as soon as the user cancels or the deadline passes."""
        if self.cancel_event.is_set():
            coro.close()  # type: ignore[attr-defined]
            raise StopJob("cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            coro.close()  # type: ignore[attr-defined]
            raise StopJob("timeout")
        work = asyncio.ensure_future(coro)
        waiter = asyncio.ensure_future(self.cancel_event.wait())
        try:
            done, _ = await asyncio.wait({work, waiter}, timeout=remaining,
                                         return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            work.cancel()
            waiter.cancel()
            raise
        if work in done:
            waiter.cancel()
            return work.result()
        work.cancel()
        waiter.cancel()
        raise StopJob("cancelled" if self.cancel_event.is_set() else "timeout")


# =============================================================== job registry

class JobManager:
    def __init__(self, settings: Settings, llm: BaseLLM, storage: Storage) -> None:
        self.settings = settings
        self.jobs: dict[str, JobState] = {}
        self.pipeline = ResearchPipeline(settings, llm, storage)

    def running_count(self) -> int:
        return sum(1 for j in self.jobs.values() if not j.finished)

    def create(self, topic: str, depth: str) -> JobState:
        job = JobState(topic, depth, self.settings.job_timeout)
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self.pipeline.run(job))
        return job

    def get(self, job_id: str) -> JobState | None:
        return self.jobs.get(job_id)

    def purge(self) -> None:
        now = time.time()
        for job_id, job in list(self.jobs.items()):
            if job.finished and now - job.created_at > self.settings.job_ttl:
                del self.jobs[job_id]

    async def cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self.purge()

    async def shutdown(self) -> None:
        tasks = [j.task for j in self.jobs.values() if j.task and not j.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# =============================================================== report helpers

_CITE_RE = re.compile(r"\[\s*([0-9۰-۹]{1,3})\s*\]")
_MULTI_CITE_RE = re.compile(r"\[\s*([0-9۰-۹]{1,3}(?:\s*[,،]\s*[0-9۰-۹]{1,3})+)\s*\]")
_SOURCES_HEAD_RE = re.compile(r"^#{1,4}\s*منابع.*\Z", re.MULTILINE | re.DOTALL)
_OUTER_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def normalize_citations(text: str, max_n: int) -> str:
    """Convert [1] / [1, 2] to [۱][۲] and drop references to non-existent sources."""
    def _multi(match: re.Match[str]) -> str:
        return "".join(f"[{n}]" for n in re.split(r"\s*[,،]\s*", match.group(1)))

    def _single(match: re.Match[str]) -> str:
        number = int(match.group(1).translate(_FA_TO_LATIN))
        return f"[{fa(number)}]" if 1 <= number <= max_n else ""

    return _CITE_RE.sub(_single, _MULTI_CITE_RE.sub(_multi, text))


def sources_markdown(sources: list[dict[str, Any]]) -> str:
    lines = ["## منابع", ""]
    for src in sources:
        host = _domain(src["url"])
        title = " ".join(re.sub(r"[\[\]()<>]", " ", src.get("title") or "").split())[:90] or host
        label = title if (not host or host in title) else f"{title} — {host}"
        url = src["url"].replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        lines.append(f"{src['n']}. [{label}]({url})")
    return "\n".join(lines)


def _insert_notes(text: str, notes: list[str]) -> str:
    if not notes:
        return text
    first, _, rest = text.partition("\n")
    quote = "\n".join(f"> {n}" for n in notes)
    return f"{first}\n\n{quote}\n{rest}"


def finalize_report(raw: str, topic: str, sources: list[dict[str, Any]],
                    notes: list[str]) -> str | None:
    """Clean the writer's output and append a deterministic sources list.

    Returns None when the output is unusable (empty or without a single citation), so the
    caller can fall back to the deterministic report.
    """
    text = strip_think(raw or "")
    fenced = _OUTER_FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)
    text = _SOURCES_HEAD_RE.sub("", text).strip()
    text = normalize_citations(text, len(sources))
    if len(text) < 80 or not _CITE_RE.search(text):
        return None
    if not text.lstrip().startswith("#"):
        text = f"# گزارش پژوهشی: {topic}\n\n{text}"
    text = _insert_notes(text.strip(), notes)
    return f"{text}\n\n{sources_markdown(sources)}\n"


def build_fallback_report(topic: str, plan: list[SubQuestion], chosen: list[Finding],
                          mapping: dict[str, int], sources: list[dict[str, Any]],
                          notes: list[str]) -> str:
    """Deterministic report used when the LLM writer fails. Every claim carries a citation."""
    lines = [f"# گزارش پژوهشی: {topic}", ""]
    lines += [f"> {n}" for n in notes] + ([""] if notes else [])
    lines += [
        "## خلاصه‌ی اجرایی",
        f"در این پژوهش {fa(len(chosen))} یافته‌ی دارای منبع از {fa(len(sources))} منبع گردآوری شد. "
        "نگارش خودکار متن با مدل زبانی ناموفق بود، بنابراین یافته‌ها بدون بازنویسی و با ارجاع به منبع فهرست شده‌اند.",
        "", "## یافته‌های کلیدی", "",
    ]
    by_sub: dict[int, list[Finding]] = {}
    for f in chosen:
        by_sub.setdefault(f.sub_index, []).append(f)
    for idx in sorted(by_sub):
        question = plan[idx].question if idx < len(plan) else f"زیرسؤال {fa(idx + 1)}"
        lines += [f"### {question}", ""]
        for f in by_sub[idx]:
            tag = " (بررسی‌نشده)" if f.status == "unchecked" else ""
            lines.append(f"- {f.claim}{tag} [{fa(mapping[f.source_url])}]")
        lines.append("")
    lines += [
        "## محدودیت‌ها و نکات نامطمئن",
        "- یافته‌ها از صفحات وب استخراج و به‌صورت خودکار راستی‌آزمایی شده‌اند؛ پیش از تصمیم‌گیری مهم، منبع اصلی را بخوانید.",
        "", "## نتیجه‌گیری",
        "برای نتیجه‌گیری دقیق‌تر، یافته‌های بالا را در کنار منابع فهرست‌شده بررسی کنید.", "",
        sources_markdown(sources), "",
    ]
    return "\n".join(lines)


def build_insufficient_report(topic: str, stats: dict[str, int], notes: list[str]) -> str:
    lines = [f"# گزارش پژوهشی: {topic}", ""]
    lines += [f"> {n}" for n in notes] + ([""] if notes else [])
    lines += [
        "## خلاصه‌ی اجرایی",
        "شواهد کافی پیدا نشد. هیچ ادعای تأییدشده‌ای برای نوشتن گزارش در دسترس نیست و از حدس‌زدن پرهیز شد.",
        "", "## یافته‌های کلیدی", "یافته‌ی تأییدشده‌ای وجود ندارد.", "",
        "## محدودیت‌ها و نکات نامطمئن",
        f"- تعداد یافته‌های استخراج‌شده: {fa(stats['findings_total'])}؛ ردشده: {fa(stats['rejected'])}.",
        "- ممکن است موتور جستجو نتایج مرتبطی نداده یا صفحات قابل‌خواندن نبوده باشند.",
        "", "## نتیجه‌گیری",
        "موضوع را دقیق‌تر بنویسید، عمق «عمیق» را امتحان کنید یا دوباره تلاش کنید.", "",
    ]
    return "\n".join(lines)


# =============================================================== pipeline

class ResearchPipeline:
    def __init__(self, settings: Settings, llm: BaseLLM, storage: Storage) -> None:
        self.settings = settings
        self.llm = llm
        self.storage = storage

    # ------------------------------------------------------------ entry point

    async def run(self, job: JobState) -> None:
        tools = WebTools(self.settings)  # per-job instance -> per-job cache
        try:
            await self._run(job, tools)
        except LLMUnavailable as exc:
            await self._error(job, str(exc), "llm_unavailable")
        except LLMError as exc:
            await self._error(job, str(exc), "llm_error")
        except asyncio.CancelledError:
            job.status = "failed"
            raise
        except Exception:  # noqa: BLE001
            log.exception("job %s crashed", job.id)
            await self._error(job, "خطای غیرمنتظره‌ای رخ داد. جزئیات در لاگ سرور ثبت شد.", "internal")
        finally:
            await job.finish()

    async def _error(self, job: JobState, message: str, code: str) -> None:
        job.status = "failed"
        await job.emit("error", message, job.progress, job.stage, code=code, final=True)

    async def _run(self, job: JobState, tools: WebTools) -> None:
        try:
            await self._plan(job)
            await self._research(job, tools)
            await self._verify(job)
        except StopJob as stop:
            job.stop_reason = stop.reason

        chosen = self._select(job)

        if job.stop_reason == "cancelled":
            await job.emit("cancelled", "پژوهش متوقف شد.", job.progress, job.stage,
                           final=not chosen)
            if not chosen:
                job.status = "cancelled"
                return
        if not job.findings:
            await self._no_findings(job)
            return
        await self._write(job, chosen)

    async def _no_findings(self, job: JobState) -> None:
        if job.searches and job.search_network_errors >= job.searches:
            message = ("اتصال به موتور جستجو برقرار نشد. اینترنت یا  را بررسی کنید، "
                       "یا در .env یک موتور جستجوی جایگزین (SEARCH_FALLBACK_URL) تعریف کنید.")
            code = "network"
        elif job.stop_reason == "timeout":
            message = "سقف زمانی تمام شد و هنوز هیچ یافته‌ای پیدا نشده بود. عمق «سریع» را امتحان کنید."
            code = "timeout"
        else:
            message = "هیچ یافته‌ی مرتبطی پیدا نشد. موضوع را دقیق‌تر یا به شکل دیگری بنویسید."
            code = "no_findings"
        await self._error(job, message, code)

    # ------------------------------------------------------------ stage 1: planner

    async def _plan(self, job: JobState) -> None:
        count = DEPTH_COUNTS[job.depth]
        await job.emit("plan", "در حال برنامه‌ریزی پژوهش…", 2, "planning", subquestions=[])
        messages = [
            {"role": "system", "content": prompts.PLANNER_SYSTEM},
            {"role": "user", "content": prompts.planner_user(job.topic, count)},
        ]
        plan: list[SubQuestion] = []
        errors: list[LLMError] = []
        for _attempt in range(3):  # first try + up to 2 corrective retries
            raw = ""
            try:
                raw = await job.guard(self.llm.complete(messages, json_mode=True, temperature=0.1))
            except LLMUnavailable:
                raise
            except LLMError as exc:
                errors.append(exc)
            plan = self._parse_plan(safe_json_parse(raw))
            if plan:
                break
            messages = messages + [
                {"role": "assistant", "content": raw[:1500] or "{}"},
                {"role": "user", "content": prompts.PLANNER_RETRY},
            ]
        if not plan and len(errors) == 3:
            raise errors[-1]  # the model is failing outright: surface the real reason
        used_default = not plan
        plan = self._pad_plan(plan[:count], job.topic, count)
        job.plan = plan
        note = "برنامه‌ی پیش‌فرض استفاده شد (خروجی مدل قابل‌استفاده نبود)." if used_default else ""
        await job.emit(
            "plan", f"برنامه‌ی پژوهش آماده شد: {fa(len(plan))} زیرسؤال. {note}".strip(), 8, "planning",
            subquestions=[{"question": q.question, "search_query": q.search_query} for q in plan],
            default_plan=used_default,
        )

    @staticmethod
    def _parse_plan(parsed: Any) -> list[SubQuestion]:
        items: Any = None
        if isinstance(parsed, dict):
            items = parsed.get("subquestions") or parsed.get("sub_questions") or parsed.get("questions")
        elif isinstance(parsed, list):
            items = parsed
        if not isinstance(items, list):
            return []
        result: list[SubQuestion] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            data = {"question": item.get("question") or item.get("subquestion"),
                    "search_query": item.get("search_query") or item.get("query")}
            try:
                sq = SubQuestion.model_validate(data)
            except ValidationError:
                continue
            if sq.question in seen:
                continue
            seen.add(sq.question)
            result.append(sq)
        return result

    @staticmethod
    def _default_plan(topic: str) -> list[SubQuestion]:
        short = " ".join(topic.split()[:8])
        aspects = [
            ("مفاهیم و تعاریف اصلی درباره‌ی «{t}» چیست؟", "{t} تعریف و مفاهیم"),
            ("وضعیت کنونی و آمار مرتبط با «{t}» چیست؟", "{t} آمار و وضعیت کنونی"),
            ("چالش‌ها و خطرهای «{t}» کدام‌اند؟", "{t} چالش‌ها"),
            ("فرصت‌ها و مزایای «{t}» کدام‌اند؟", "{t} فرصت‌ها و مزایا"),
            ("روندها و پیش‌بینی‌های آینده درباره‌ی «{t}» چیست؟", "{t} آینده و پیش‌بینی"),
            ("چه پژوهش‌ها و دیدگاه‌های معتبری درباره‌ی «{t}» وجود دارد؟", "{t} پژوهش"),
        ]
        return [SubQuestion(question=q.format(t=short), search_query=s.format(t=short))
                for q, s in aspects]

    def _pad_plan(self, plan: list[SubQuestion], topic: str, count: int) -> list[SubQuestion]:
        if len(plan) >= count:
            return plan
        for extra in self._default_plan(topic):
            if len(plan) >= count:
                break
            plan.append(extra)
        return plan

    # ------------------------------------------------------------ stage 2: researcher

    @staticmethod
    def _candidates(results: list[dict[str, str]], limit: int = 5) -> list[dict[str, str]]:
        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for item in results:
            url = item["url"]
            if urlparse(url).path.lower().endswith(SKIP_EXTENSIONS):
                continue
            host = _domain(url)
            if not host or host in seen:
                continue
            seen.add(host)
            out.append(item)
            if len(out) >= limit:
                break
        return out

    async def _research(self, job: JobState, tools: WebTools) -> None:
        total = len(job.plan)
        span = 52 / total
        for index, sq in enumerate(job.plan):
            base = int(8 + span * index)
            await job.emit(
                "subquestion_start",
                f"زیرسؤال {fa(index + 1)} از {fa(total)}: {sq.question}", base, "gathering",
                index=index, question=sq.question, query=sq.search_query)
            await job.emit("tool_call", f"جستجو در وب: «{sq.search_query}»", base, "gathering",
                           tool="search_web", query=sq.search_query)
            job.searches += 1
            search = await job.guard(tools.search_web(sq.search_query, 8))
            if not search["ok"]:
                if search.get("code") == "network":
                    job.search_network_errors += 1
                await job.emit("tool_call", f"جستجو ناموفق بود: {search['error']}", base, "gathering",
                               tool="search_web", ok=False)
                continue

            await job.emit(
                "tool_call",
                f"نتایج جستجو از «{search.get('provider', '?')}» دریافت شد: {fa(len(search['results']))} نتیجه",
                base, "gathering", tool="search_web", ok=True, provider=search.get("provider"))
            read_ok = 0
            for cand in self._candidates(search["results"]):
                if read_ok >= PAGES_PER_QUESTION:
                    break
                progress = int(base + span * (0.25 + 0.3 * read_ok))
                await job.emit("tool_call", f"خواندن صفحه: {_domain(cand['url'])}", progress,
                               "gathering", tool="read_webpage", url=cand["url"])
                page = await job.guard(tools.read_webpage(cand["url"], PAGE_CHARS_FOR_LLM))
                if not page["ok"]:
                    await job.emit("tool_call", f"صفحه خوانده نشد ({_domain(cand['url'])}): {page['error']}",
                                   progress, "gathering", tool="read_webpage", ok=False)
                    continue
                read_ok += 1
                raws = await self._extract(job, sq, page)
                if raws is None:
                    await job.emit("tool_call", "استخراج یافته از این صفحه ناموفق بود (خروجی مدل نامعتبر).",
                                   progress, "gathering", tool="extract", ok=False)
                    continue
                for raw in raws:
                    finding = Finding(
                        id=len(job.findings) + 1, sub_index=index, claim=raw.claim,
                        evidence=raw.evidence, source_url=page["url"],  # URL comes from the tool, not the model
                        source_title=page["title"] or _domain(page["url"]),
                    )
                    job.findings.append(finding)
                    await job.emit(
                        "finding", f"یافته‌ی جدید: {finding.claim}",
                        int(base + span * 0.85), "gathering", finding=finding.model_dump())

    async def _extract(self, job: JobState, sq: SubQuestion, page: dict[str, Any]) -> list[RawFinding] | None:
        messages = [
            {"role": "system", "content": prompts.EXTRACTOR_SYSTEM},
            {"role": "user", "content": prompts.extractor_user(sq.question, page["title"], page["url"], page["text"])},
        ]
        for _attempt in range(2):
            try:
                raw = await job.guard(self.llm.complete(messages, json_mode=True, temperature=0.1))
            except LLMUnavailable:
                raise
            except LLMError:
                return None
            parsed = safe_json_parse(raw)
            items = parsed.get("findings") if isinstance(parsed, dict) else parsed
            if isinstance(items, list):
                out: list[RawFinding] = []
                seen: set[str] = set()
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        rf = RawFinding.model_validate(
                            {"claim": item.get("claim"), "evidence": item.get("evidence")})
                    except ValidationError:
                        continue
                    if rf.claim in seen:
                        continue
                    seen.add(rf.claim)
                    out.append(rf)
                    if len(out) >= MAX_FINDINGS_PER_PAGE:
                        break
                return out  # an empty list is a legitimate "I don't know"
            messages = messages + [
                {"role": "assistant", "content": raw[:1500] or "{}"},
                {"role": "user", "content": prompts.EXTRACTOR_RETRY},
            ]
        return None

    # ------------------------------------------------------------ stage 3: verifier

    async def _verify(self, job: JobState) -> None:
        total = len(job.findings)
        if total == 0:
            return
        await job.emit("verification", f"شروع راستی‌آزمایی {fa(total)} یافته…", 62, "verifying",
                       started=True, total=total)
        for i, finding in enumerate(job.findings):
            progress = 62 + int(30 * (i + 1) / total)
            verdict = await self._judge(job, finding)
            if verdict is None:
                # A parse failure must NEVER count as "verified".
                finding.status = "unchecked"
                finding.confidence = None
                finding.reason = "پاسخ راستی‌آزما قابل‌پردازش نبود؛ بررسی‌نشده"
            else:
                finding.confidence = verdict.confidence
                finding.reason = verdict.reason
                finding.status = "verified" if (verdict.supported and verdict.confidence >= 0.5) else "rejected"
            label = {"verified": "تأییدشد", "rejected": "ردشد", "unchecked": "بررسی‌نشد"}[finding.status]
            await job.emit(
                "verification", f"یافته‌ی {fa(finding.id)} {label}. {finding.reason}".strip(),
                progress, "verifying", id=finding.id, status=finding.status,
                confidence=finding.confidence, reason=finding.reason)

    async def _judge(self, job: JobState, finding: Finding) -> Verdict | None:
        messages = [
            {"role": "system", "content": prompts.VERIFIER_SYSTEM},
            {"role": "user", "content": prompts.verifier_user(finding.claim, finding.evidence)},
        ]
        for _attempt in range(2):
            try:
                raw = await job.guard(self.llm.complete(messages, json_mode=True, temperature=0.1))
            except LLMUnavailable:
                raise
            except LLMError:
                return None
            parsed = safe_json_parse(raw)
            if isinstance(parsed, dict):
                try:
                    return Verdict.model_validate(parsed)
                except ValidationError:
                    pass
            messages = messages + [
                {"role": "assistant", "content": raw[:1500] or "{}"},
                {"role": "user", "content": prompts.VERIFIER_RETRY},
            ]
        return None

    # ------------------------------------------------------------ stage 4: writer

    @staticmethod
    def _select(job: JobState) -> list[Finding]:
        verified = [f for f in job.findings if f.status == "verified"]
        unchecked = [f for f in job.findings if f.status == "unchecked"]
        chosen = verified if len(verified) >= MIN_VERIFIED else verified + unchecked
        if len(chosen) > MAX_WRITER_FINDINGS:
            best = sorted(chosen, key=lambda f: (f.status != "verified", -(f.confidence or 0.0)))
            chosen = sorted(best[:MAX_WRITER_FINDINGS], key=lambda f: f.id)
        return chosen

    @staticmethod
    def _build_sources(chosen: list[Finding]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        sources: list[dict[str, Any]] = []
        mapping: dict[str, int] = {}
        for f in sorted(chosen, key=lambda f: (f.sub_index, f.id)):
            if f.source_url not in mapping:
                mapping[f.source_url] = len(sources) + 1
                sources.append({"n": mapping[f.source_url], "url": f.source_url,
                                "title": f.source_title or _domain(f.source_url)})
        return sources, mapping

    @staticmethod
    def _stats(job: JobState, sources: list[dict[str, Any]]) -> dict[str, int]:
        count = lambda s: sum(1 for f in job.findings if f.status == s)  # noqa: E731
        return {"findings_total": len(job.findings), "verified": count("verified"),
                "rejected": count("rejected"), "unchecked": count("unchecked"),
                "sources": len(sources)}

    async def _write(self, job: JobState, chosen: list[Finding]) -> None:
        partial = job.stop_reason is not None
        job.partial = partial
        notes: list[str] = []
        if job.stop_reason == "timeout":
            notes.append("سقف زمانی پژوهش تمام شد؛ این گزارش جزئی است و فقط یافته‌های تأییدشده‌ی تا آن لحظه را دارد.")
        elif job.stop_reason == "cancelled":
            notes.append("پژوهش پیش از پایان متوقف شد؛ این گزارش جزئی است.")
        unchecked_used = any(f.status == "unchecked" for f in chosen)
        if unchecked_used:
            notes.append("به‌دلیل کمبود یافته‌ی تأییدشده، برخی یافته‌ها با برچسب «بررسی‌نشده» آمده‌اند.")

        sources, mapping = self._build_sources(chosen)
        stats = self._stats(job, sources)
        stage_msg = "سقف زمانی تمام شد؛ در حال نگارش گزارش جزئی…" if partial else "در حال نگارش گزارش نهایی…"
        await job.emit("writing", stage_msg, 94, "writing", partial=partial)

        if not chosen:
            report = build_insufficient_report(job.topic, stats, notes)
        else:
            report = await self._llm_report(job, chosen, sources, mapping, notes, partial)
        job.report_md = report
        job.sources = sources
        job.status = "completed"
        try:
            await self.storage.save_report(job.id, job.topic, job.depth, report, sources, stats, partial)
        except Exception:  # noqa: BLE001
            log.exception("could not save report %s", job.id)
        await job.emit("complete", "گزارش آماده است.", 100, "writing",
                       report=report, sources=sources, partial=partial, stats=stats,
                       report_id=job.id, final=True)

    async def _llm_report(self, job: JobState, chosen: list[Finding], sources: list[dict[str, Any]],
                          mapping: dict[str, int], notes: list[str], partial: bool) -> str:
        by_sub: dict[int, list[Finding]] = {}
        for f in sorted(chosen, key=lambda f: (f.sub_index, f.id)):
            by_sub.setdefault(f.sub_index, []).append(f)
        blocks: list[tuple[str, list[str]]] = []
        for idx, items in sorted(by_sub.items()):
            question = job.plan[idx].question if idx < len(job.plan) else f"زیرسؤال {fa(idx + 1)}"
            lines = [
                f"- [{fa(mapping[f.source_url])}] ({'تأییدشده' if f.status == 'verified' else 'بررسی‌نشده'}) "
                f"ادعا: {f.claim} | شاهد: {f.evidence}"
                for f in items
            ]
            blocks.append((question, lines))
        messages = [
            {"role": "system", "content": prompts.WRITER_SYSTEM},
            {"role": "user", "content": prompts.writer_user(job.topic, blocks, partial)},
        ]
        try:
            raw = await asyncio.wait_for(
                self.llm.complete(messages, json_mode=False, temperature=0.3), timeout=WRITER_TIMEOUT)
            report = finalize_report(raw, job.topic, sources, notes)
            if report:
                return report
            log.warning("writer output unusable for job %s; using fallback report", job.id)
        except (LLMError, asyncio.TimeoutError):
            log.warning("writer failed for job %s; using fallback report", job.id)
        return build_fallback_report(job.topic, job.plan, chosen, mapping, sources, notes)
