#!/usr/bin/env python3
"""Local, review-gated agent knowledge. No network or model calls in this CLI."""
from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

ZONE = ZoneInfo("Europe/Moscow")
METRICS = ("outcome", "user_corrections", "user_interventions", "review_escapes", "rework", "lesson_application")
SAFE_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,120}$")
EMPTY_KB = "# Knowledge base\n\nNo approved knowledge yet.\n"


def now():
    return datetime.now(ZONE).isoformat(timespec="seconds")


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex[:16]


def normalized(text):
    return " ".join(str(text).lower().split())


def entry_snapshot(entry):
    if not isinstance(entry, dict) or any(key not in entry for key in ("id", "text", "lesson_ids", "lesson_versions")):
        raise ValueError("An exact original entry snapshot is required")
    return {key: entry[key] for key in ("id", "text", "lesson_ids", "lesson_versions")}


def mask(text):
    text = re.sub(r"\b(?:y[01]_|t[01]\.|AQAD-)[A-Za-z0-9_.-]{12,}", "<credential-masked>", text)
    text = re.sub(r"(?i)\b(Bearer|Api-Key)\s+[A-Za-z0-9_.+/=-]{8,}", r"\1 <credential-masked>", text)
    # Access-bearing URLs retain only their origin/path.
    text = re.sub(r"https?://[^\s<>\]\)]+", lambda m: re.split(r"[?#]", m[0])[0] + "?QUERY_MASKED" if "?" in m[0] else m[0], text)
    return text


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".kb-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class KB:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.internal = self.root / ".aggregator"
        self.state_path = self.internal / "state.json"

    def path(self, relative):
        p = Path(relative)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("Path must be relative to the KB")
        target = self.root / p
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("Path escapes the KB")
        for parent in [target, *target.parents]:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ValueError("Managed paths must not be symlinks")
        return target

    def read(self, relative, default=""):
        p = self.path(relative)
        return p.read_text() if p.is_file() else default

    @contextlib.contextmanager
    def locked(self, readonly=False):
        if not self.state_path.exists():
            raise ValueError("Initialize the KB first")
        with self.path(".aggregator/lock").open("r" if readonly else "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH if readonly else fcntl.LOCK_EX)
            transaction = self.path(".aggregator/transaction.json")
            if transaction.exists():
                if readonly:
                    raise ValueError("Publication recovery pending; run status --recover in the KB workspace")
                self.recover()
            yield json.loads(self.state_path.read_text())

    def recover(self):
        p = self.path(".aggregator/transaction.json")
        if not p.exists():
            return
        for relative, value in json.loads(p.read_text())["writes"].items():
            atomic(self.path(relative), value)
        p.unlink()

    def commit(self, state, writes=None):
        writes = dict(writes or {})
        writes[".aggregator/state.json"] = json_text(state)
        for relative in writes:
            self.path(relative)
        atomic(self.path(".aggregator/transaction.json"), json_text({"writes": writes}))
        self.recover()

    def init(self, source):
        if self.state_path.exists():
            return {"status": "already_initialized"}
        source = Path(source).expanduser().resolve()
        if not source.is_dir() or source == self.root:
            raise ValueError("Session source must be an existing separate directory")
        self.internal.mkdir(parents=True, mode=0o700)
        atomic(self.path(".aggregator/lock"), "")
        state = {"schema_version": 1, "created_at": now(), "source": str(source),
                 "initial_cutoff": (datetime.now(ZONE) - timedelta(days=30)).date().isoformat(),
                 "projects": {}, "threads": {}, "sessions": {}, "proposals": {},
                 "active": {"global": []}, "published": {"global": digest(EMPTY_KB)},
                 "project_inputs": {}, "last_global_input": None, "last_global_date": None,
                 "run": None, "decisions": []}
        self.commit(state, {"knowledge-base.md": EMPTY_KB, "review/inbox.md": "# Review inbox\n\nNo pending proposals.\n"})
        return {"status": "initialized", "root": str(self.root)}

    def target(self, state, scope):
        if scope == "global":
            return "knowledge-base.md"
        if scope not in state["projects"]:
            raise ValueError("Unknown project scope")
        return "projects/" + state["projects"][scope]["slug"] + "/knowledge-base.md"

    def check_published(self, state, scope):
        if digest(self.read(self.target(state, scope))) != state["published"][scope]:
            raise ValueError("Active KB changed outside publication workflow: " + scope)

    def inventory(self, state, inventory, writes):
        for project in inventory.get("projects", []):
            if project.get("projectKind") != "local" or not project.get("path"):
                continue
            path = str(Path(project["path"]).expanduser().resolve())
            if Path(path) == self.root:
                continue
            if Path(path) in (Path("/"), Path.home()):
                continue
            pid = str(project["projectId"])
            if not SAFE_ID.fullmatch(pid):
                raise ValueError("Invalid project ID")
            entry = state["projects"].get(pid)
            if not entry:
                label = "zapravki-backend" if path.endswith("/zapravki/backend") else project.get("label", Path(path).name)
                slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")[:50] or "project"
                if any(p["slug"] == slug for p in state["projects"].values()):
                    slug += "-" + pid[:8]
                entry = {"id": pid, "label": project.get("label", slug), "slug": slug, "paths": []}
                state["projects"][pid] = entry
                state["active"][pid] = []
                state["published"][pid] = digest(EMPTY_KB)
                writes[self.target(state, pid)] = EMPTY_KB
            if path not in entry["paths"]:
                entry["paths"].append(path)
            writes["projects/" + entry["slug"] + "/project.json"] = json_text(entry)
        for thread in inventory.get("threads", []):
            sid = thread.get("id")
            if not isinstance(sid, str) or not SAFE_ID.fullmatch(sid):
                continue
            pid, cwd = thread.get("projectId"), thread.get("cwd")
            excluded = bool(cwd and Path(cwd).resolve().is_relative_to(self.root))
            if pid not in state["projects"] and cwd and not excluded:
                matches = [(len(Path(p).parts), project_id) for project_id, project in state["projects"].items()
                           for p in project["paths"] if Path(cwd).resolve().is_relative_to(Path(p))]
                pid = max(matches)[1] if matches else None
            state["threads"][sid] = {"project_id": pid if pid in state["projects"] else None,
                                      "cwd": cwd, "excluded": excluded}
        return state

    def begin(self, inventory):
        with self.locked() as state:
            previous = state.get("run")
            if previous and previous["expires_at"] > time.time():
                return {"status": "busy", "run_id": previous["id"], "expires_at": previous["expires_at"]}
            writes = {}
            if previous:
                writes[f".aggregator/runs/{previous['id']}/abandoned.json"] = json_text({"at": now(), "reason": "lease_expired"})
            self.inventory(state, inventory, writes)
            rid = identifier("run")
            state["run"] = {"id": rid, "started_at": now(), "expires_at": time.time() + 7200, "jobs": {}}
            self.commit(state, writes)
            return {"status": "started", "run_id": rid}

    def current_run(self, state, rid):
        run = state.get("run")
        if not run or run["id"] != rid or run["expires_at"] <= time.time():
            raise ValueError("Run lease missing, expired, or superseded")
        return run

    def source_note(self, path):
        if path.is_symlink() or path.stat().st_size > 1_000_000:
            raise ValueError("Source note is a symlink or exceeds 1 MB")
        text = mask(path.read_text())
        match = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
        metadata = yaml.safe_load(match[1]) if match else {}
        if not isinstance(metadata, dict):
            raise ValueError("Invalid note metadata")
        return text, metadata

    def session_jobs(self, state, writes):
        jobs, unassigned, errors, pending = [], [], [], 0
        root = Path(state["source"])
        paths = sorted(root.glob("*.md"), key=lambda p: (p.stat().st_mtime, p.name))
        for path in paths:
            try:
                text, metadata = self.source_note(path)
                sid = str(metadata.get("session_id") or "")
                if not SAFE_ID.fullmatch(sid):
                    continue
                thread = state["threads"].get(sid, {})
                if thread.get("excluded"):
                    continue
                existing = state["sessions"].get(sid)
                day = str(metadata.get("date", ""))[:10]
                if not existing and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or day < state["initial_cutoff"]):
                    continue
                sha = digest(text)
                if existing and existing.get("processed_hash") == sha:
                    continue
                if path.stat().st_mtime > time.time() - 300:
                    continue
                pid = thread.get("project_id")
                if pid not in state["projects"]:
                    unassigned.append({"session_id": sid, "path": str(path), "title": str(metadata.get("title", ""))})
                    continue
                pending += 1
                if len(jobs) >= 10:
                    continue
                folder = existing["folder"] if existing else f"projects/{state['projects'][pid]['slug']}/sessions/{day}--{sid}"
                record = dict(existing or {}, id=sid, project_id=pid, folder=folder,
                              source_path=str(path), source_hash=sha, source_date=day,
                              title=str(metadata.get("title", "")), lessons=(existing or {}).get("lessons", []),
                              source_versions=list(dict.fromkeys((existing or {}).get("source_versions", []) + [sha])))
                if existing and existing["project_id"] != pid:
                    raise ValueError("Session project changed; explicit reassignment required")
                state["sessions"][sid] = record
                writes[f".aggregator/sources/{sha}.md"] = text
                writes[folder + "/session.md"] = text
                if not self.path(folder + "/assessments.md").exists():
                    writes[folder + "/assessments.md"] = "# Assessments\n\nAppend-only. Pending assessments do not alter active guidance.\n"
                jobs.append({"stage": "session", "session_id": sid, "project_id": pid, "input_hash": sha,
                             "source": text, "existing_lessons": record["lessons"],
                             "assessments": self.read(folder + "/assessments.md"),
                             "thread_id": sid, "evidence_policy": "digest first; targeted original messages if needed; no reasoning records"})
            except (ValueError, OSError, yaml.YAMLError) as exc:
                errors.append({"path": str(path), "error": str(exc)[:300]})
        writes["review/unassigned.md"] = "# Unassigned sessions\n\n" + ("\n".join(f"- `{x['session_id']}` — {x['title']} — `{x['path']}`" for x in unassigned) or "None.") + "\n"
        state["backlog"] = max(0, pending - len(jobs))
        state["source_errors"] = errors
        return jobs

    def scope_input(self, state, scope):
        if scope == "global":
            global_lids = {lid for e in state["active"]["global"] for lid in e["lesson_ids"]}
            assessments = [{"session_id": s["id"], "assessments": self.read(s["folder"] + "/assessments.md")}
                           for s in state["sessions"].values() if global_lids.intersection(l["id"] for l in s["lessons"])]
            return {"approved_projects": {p: state["active"][p] for p in sorted(state["projects"]) if state["active"][p]},
                    "active": state["active"]["global"], "decisions": state["decisions"], "assessments": assessments}
        sessions = [s for s in state["sessions"].values() if s["project_id"] == scope and s.get("processed_hash")]
        return {"sessions": [{"id": s["id"], "lessons": s["lessons"],
                               "assessments": self.read(s["folder"] + "/assessments.md")} for s in sessions],
                "active": state["active"][scope],
                "prior_proposals": [{"id": p["id"], "status": p["status"], "entries": p["entries"], "reason": p.get("review_reason", "")}
                                    for p in state["proposals"].values() if p["scope"] == scope]}

    def prepare(self, rid, stage):
        with self.locked() as state:
            run = self.current_run(state, rid)
            if any(j["status"] == "pending" for j in run["jobs"].values()):
                raise ValueError("Record existing jobs before preparing another stage")
            writes = {}
            if stage == "session":
                jobs = self.session_jobs(state, writes)
            else:
                jobs = []
                scopes = list(state["projects"]) if stage == "project" else ["global"]
                today = datetime.now(ZONE)
                global_assessments = self.scope_input(state, "global")["assessments"] if stage == "global" else []
                priority = bool(global_assessments) and digest(global_assessments) != state.get("last_global_assessments")
                if stage == "global" and not priority and (today.hour < 9 or state["last_global_date"] == today.date().isoformat()):
                    scopes = []
                for scope in scopes:
                    self.check_published(state, scope)
                    inputs = self.scope_input(state, scope)
                    sha = digest(inputs)
                    previous = state["last_global_input"] if scope == "global" else state["project_inputs"].get(scope)
                    has_inputs = bool(inputs["approved_projects"] or inputs["active"]) if scope == "global" else bool(inputs["sessions"])
                    if previous == sha or not has_inputs:
                        if scope == "global":
                            state["last_global_date"] = today.date().isoformat()
                            state["last_global_input"] = sha
                        continue
                    jobs.append({"stage": stage, "scope": scope, "input_hash": sha,
                                 "base_hash": state["published"][scope], "inputs": inputs})
            for job in jobs:
                jid = identifier("job")
                job["id"] = jid
                run["jobs"][jid] = {"stage": stage, "status": "pending", "input_hash": job["input_hash"],
                                     "session_id": job.get("session_id"), "scope": job.get("scope")}
                writes[f".aggregator/runs/{rid}/{jid}.json"] = json_text(job)
            self.commit(state, writes)
            return {"run_id": rid, "jobs": [{"id": j["id"], "path": str(self.path(f'.aggregator/runs/{rid}/{j["id"]}.json'))} for j in jobs],
                    "backlog": state.get("backlog", 0), "source_errors": state.get("source_errors", [])}

    def evidence(self, state, evidence, allowed_session=None, extra=None):
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Evidence is required")
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item.get("quote"), str) or len(item["quote"].strip()) < 8:
                raise ValueError("Evidence needs a meaningful exact quote")
            sid, sha = item.get("session_id"), item.get("source_hash")
            session = state["sessions"].get(sid)
            if not session or (allowed_session and sid != allowed_session) or sha not in session.get("source_versions", []):
                raise ValueError("Evidence session is not allowed")
            if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
                raise ValueError("Invalid source hash")
            source = self.read(f".aggregator/sources/{sha}.md")
            if not source or item["quote"] not in source:
                raise ValueError("Evidence quote is absent from preserved source")

    def all_lessons(self, state):
        return {l["id"]: l for s in state["sessions"].values() for l in s["lessons"]}

    def excerpt(self, sid, payload):
        """Preserve selected connector records, never internal reasoning records."""
        with self.locked() as state:
            if sid not in state["sessions"] or payload.get("thread_id") != sid:
                raise ValueError("Excerpt must match a registered session")
            items = payload.get("items", [])
            allowed = {"userMessage", "agentMessage", "commandExecution", "mcpToolCall"}
            selected = []
            for item in items:
                if item.get("type") not in allowed:
                    continue
                if not item.get("turn_id") or not item.get("id") or not isinstance(item.get("text"), str):
                    raise ValueError("Excerpts require turn/item IDs and explicit text")
                selected.append({k: item.get(k) for k in ("type", "turn_id", "id", "text", "truncated")})
            if not selected or len(json_text(selected)) > 100000:
                raise ValueError("Expected bounded, relevant original excerpts")
            selected = json.loads(mask(json_text(selected)))
            text = f"# Original session excerpts\n\nSession: {sid}\nCoverage: selected records only; truncated flags preserved.\n"
            for item in selected:
                text += f"\n## {item['turn_id']} / {item['id']} ({item['type']}; truncated={item['truncated']})\n\n{item['text']}\n"
            sha = digest(text)
            session = state["sessions"][sid]
            session["source_versions"] = list(dict.fromkeys(session.get("source_versions", []) + [sha]))
            self.commit(state, {f".aggregator/sources/{sha}.md": text,
                                f".aggregator/evidence/{sha}.json": json_text({"thread_id": sid, "items": selected})})
            return {"session_id": sid, "source_hash": sha, "path": str(self.path(f".aggregator/sources/{sha}.md"))}

    def render_lessons(self, session):
        text = "# Extracted lessons\n\nCandidates only; assessments record subsequent decisions.\n"
        for lesson in session["lessons"]:
            text += f"\n## {lesson['id']}\n\n{lesson['statement']}\n\nApplies when: {lesson['condition']}\n\nRevision: `{lesson['version']}`\n"
            for e in lesson["evidence"]:
                text += f"\n> {e['quote']}\n\nSource: `{e['session_id']}` / `{e['source_hash']}`\n"
        return text

    def append_assessment(self, state, writes, sid, event):
        folder = state["sessions"][sid]["folder"]
        relative = folder + "/assessments.md"
        current = writes.get(relative, self.read(relative))
        writes[relative] = current + f"\n## {event['id']} — {event['decision']}\n\n```json\n{json_text(event).rstrip()}\n```\n"

    def record_session(self, state, job, result, writes):
        session = state["sessions"][job["session_id"]]
        if session["source_hash"] != job["input_hash"]:
            raise ValueError("Source revision superseded")
        current, _ = self.source_note(Path(session["source_path"]))
        if digest(current) != job["input_hash"]:
            raise ValueError("Source changed during analysis; retry next run")
        items = result.get("lessons")
        if not isinstance(items, list) or len(items) > 20:
            raise ValueError("Expected at most 20 lesson candidates")
        existing = {l["id"]: l for l in session["lessons"]}
        by_text = {normalized(l["statement"]): l["id"] for l in session["lessons"]}
        for item in items:
            statement, condition = item.get("statement"), item.get("condition")
            if not isinstance(statement, str) or not 10 <= len(statement) <= 2000 or not isinstance(condition, str) or not condition.strip():
                raise ValueError("Lesson needs a bounded statement and applicability condition")
            self.evidence(state, item.get("evidence"), session["id"])
            lid = item.get("id") or by_text.get(normalized(statement)) or identifier("lesson")
            if item.get("id") and lid not in existing:
                raise ValueError("A supplied lesson ID must already belong to this session")
            lesson = {"id": lid, "statement": statement, "condition": condition, "evidence": item["evidence"]}
            lesson["version"] = digest({k: v for k, v in lesson.items() if k != "id"})
            existing[lid] = lesson
            by_text[normalized(statement)] = lid
            writes[f".aggregator/lesson-revisions/{lesson['version']}.json"] = json_text(lesson)
        session["lessons"] = list(existing.values())
        session["processed_hash"] = job["input_hash"]
        metrics = result.get("metrics", {})
        if not isinstance(metrics, dict) or set(metrics) - set(METRICS):
            raise ValueError("Unknown metrics")
        for value in metrics.values():
            if value is None:
                continue
            if not isinstance(value, dict) or value.get("kind") not in ("observed", "inferred") or "value" not in value:
                raise ValueError("Metrics require value, kind, and evidence")
            self.evidence(state, value.get("evidence"), session["id"])
        writes[session["folder"] + "/lessons.md"] = self.render_lessons(session)
        writes[session["folder"] + "/metrics.json"] = json_text({"schema_version": 1, "session_id": session["id"],
            "source_hash": job["input_hash"], "coverage": "digest_and_any_explicitly_preserved_excerpts; not all sessions have digests",
            "metrics": {key: metrics.get(key) for key in METRICS}, "recorded_at": now()})
        for assessment in result.get("assessments", []):
            owners = {l["id"]: s["id"] for s in state["sessions"].values() for l in s["lessons"]}
            if assessment.get("lesson_id") not in owners or assessment.get("decision") not in ("disputed", "invalidated", "superseded", "confirmed"):
                raise ValueError("Invalid assessment")
            self.evidence(state, assessment.get("evidence"), session["id"])
            if not assessment.get("reason"):
                raise ValueError("Assessment needs a reason")
            event = dict(assessment, id=identifier("assessment"), status="pending", at=now())
            self.append_assessment(state, writes, owners[assessment["lesson_id"]], event)

    def render_kb(self, entries, scope="global"):
        if not entries:
            return EMPTY_KB
        text = "# Knowledge base\n\nApproved guidance; explicit current instructions take precedence.\n"
        prefix = "" if scope == "global" else "../../"
        for entry in entries:
            text += f"\n## {entry['id']}\n\n{entry['text']}\n\nLessons: " + ", ".join(f"`{lid}`" for lid in entry["lesson_ids"]) + "\n"
            for evidence in entry.get("evidence", []):
                text += f"\n[Source {evidence['session_id']}]({prefix}.aggregator/sources/{evidence['source_hash']}.md)\n"
        return text

    def validate_entries(self, state, scope, entries):
        if not isinstance(entries, list) or len(entries) > 50:
            raise ValueError("Expected at most 50 KB entries")
        lessons = self.all_lessons(state)
        ids, output = set(), []
        project_lessons = {l["id"] for s in state["sessions"].values() if s["project_id"] == scope for l in s["lessons"]}
        global_versions = {lid: version for pid in state["projects"] for e in state["active"][pid] for lid, version in e.get("lesson_versions", {}).items()}
        global_lessons = set(global_versions)
        existing = {e["id"]: e for e in state["active"][scope]}
        for entry in entries:
            text, lids = entry.get("text"), entry.get("lesson_ids")
            eid = entry.get("id") or identifier("entry")
            if not SAFE_ID.fullmatch(eid) or eid in ids:
                raise ValueError("Invalid or duplicate entry ID")
            if not isinstance(text, str) or not 10 <= len(text) <= 3000 or not isinstance(lids, list) or not lids:
                raise ValueError("Entry needs bounded text and supporting lessons")
            allowed = global_lessons if scope == "global" else project_lessons
            old = existing.get(eid)
            unchanged = old and old["text"] == text and sorted(old["lesson_ids"]) == sorted(set(lids))
            if unchanged:
                allowed = allowed | set(old["lesson_ids"])
            if any(lid not in lessons or lid not in allowed for lid in lids):
                raise ValueError("Entry has missing or out-of-scope lesson evidence")
            ids.add(eid)
            versions = dict(old["lesson_versions"]) if unchanged else {lid: (global_versions[lid] if scope == "global" else lessons[lid]["version"]) for lid in lids}
            if entry.get("lesson_versions") and entry["lesson_versions"] != versions:
                raise ValueError("Entry supporting revisions changed")
            evidence = []
            for lid, version in versions.items():
                archive = self.read(f".aggregator/lesson-revisions/{version}.json")
                record = json.loads(archive) if archive else lessons[lid]
                evidence.extend(record["evidence"])
            result = {"id": eid, "text": text, "lesson_ids": sorted(set(lids)), "lesson_versions": versions,
                      "evidence": list({digest(e): e for e in evidence}.values())}
            replacements = entry.get("replaces", (old or {}).get("replaces"))
            if replacements is not None:
                conditions = entry.get("merge_conditions", (old or {}).get("merge_conditions"))
                if not isinstance(replacements, list) or len(replacements) < 2 or not isinstance(conditions, str) or not conditions.strip() or len(conditions) > 2000:
                    raise ValueError("A merge needs at least two originals and compatible conditions")
                snapshots = [entry_snapshot(original) for original in replacements]
                replaced_ids = [original["id"] for original in snapshots]
                if len(replaced_ids) != len(set(replaced_ids)) or eid in replaced_ids or any(not isinstance(x, str) or not SAFE_ID.fullmatch(x) for x in replaced_ids):
                    raise ValueError("Merge originals must have distinct IDs different from the merged entry")
                union = {}
                for original in snapshots:
                    if not isinstance(original["text"], str) or not original["text"] or not isinstance(original["lesson_ids"], list) or not original["lesson_ids"] or not isinstance(original["lesson_versions"], dict) or set(original["lesson_ids"]) != set(original["lesson_versions"]):
                        raise ValueError("Invalid original entry snapshot")
                    for lid, version in original["lesson_versions"].items():
                        if lid in union and union[lid] != version:
                            raise ValueError("Merge originals disagree about a supporting lesson version")
                        union[lid] = version
                if union != versions:
                    raise ValueError("Merged evidence must be exactly the union of original lesson versions")
                established = old and old.get("replaces") == snapshots
                if not established:
                    available = {e["id"]: e for e in state["active"][scope]}
                    for proposal in state["proposals"].values():
                        if proposal["scope"] == scope and proposal["status"] == "pending":
                            for candidate in proposal["entries"]:
                                available.setdefault(candidate["id"], candidate)
                    for original in snapshots:
                        current = available.get(original["id"])
                        if not current or entry_snapshot(current) != original:
                            raise ValueError("Merge original changed, is missing, or belongs to another scope")
                result.update(replaces=snapshots, merge_conditions=conditions)
            output.append(result)
        replacement_ids = [original["id"] for result in output for original in result.get("replaces", [])]
        if len(replacement_ids) != len(set(replacement_ids)):
            raise ValueError("Merge replacements overlap")
        if set(replacement_ids) & {result["id"] for result in output}:
            raise ValueError("A merged entry and its originals cannot coexist")
        if len(self.render_kb(output, scope)) > 20000:
            raise ValueError("Consolidated KB exceeds the 20,000-character runtime budget")
        return output

    def record_scope(self, state, job, result, writes, supersede_ids=None):
        scope = job["scope"]
        self.check_published(state, scope)
        if digest(self.scope_input(state, scope)) != job["input_hash"]:
            raise ValueError("Consolidation inputs changed; retry next run")
        if result.get("no_change") is True:
            if not result.get("reason"):
                raise ValueError("No-change result needs a reason")
        else:
            entries = self.validate_entries(state, scope, result.get("entries"))
            if not isinstance(result.get("reason"), str) or not result["reason"].strip():
                raise ValueError("Proposal needs a rationale")
            if scope == "global" and not result.get("generalization"):
                raise ValueError("Global promotion needs a generalization argument")
            # Reuse existing IDs for unchanged text, including rejected proposals.
            known = {normalized(e["text"]): e["id"] for p in state["proposals"].values() if p["scope"] == scope for e in p["entries"]}
            known.update({normalized(e["text"]): e["id"] for e in state["active"][scope]})
            for e in entries:
                if not e.get("replaces"):
                    e["id"] = known.get(normalized(e["text"]), e["id"])
            content = self.render_kb(entries, scope)
            relevant_lids = {lid for e in entries + state["active"][scope] for lid in e["lesson_ids"]}
            assessment_versions = {s["id"]: digest(self.read(s["folder"] + "/assessments.md")) for s in state["sessions"].values()
                                   if relevant_lids.intersection(l["id"] for l in s["lessons"])}
            fingerprint_input = {"scope": scope, "base_hash": job["base_hash"],
                                  "entries": [{"text": normalized(e["text"]), "lesson_ids": e["lesson_ids"]} for e in entries],
                                  "assessment_versions": assessment_versions}
            if job.get("draft_request"):
                fingerprint_input["draft_request"] = job["draft_request"]
            if any(e.get("replaces") for e in entries):
                fingerprint_input["merges"] = [{"replaces": e["replaces"], "conditions": e["merge_conditions"]} for e in entries if e.get("replaces")]
            fingerprint = digest(fingerprint_input)
            duplicate = any(p["fingerprint"] == fingerprint for p in state["proposals"].values())
            if content != self.read(self.target(state, scope)) and not duplicate:
                pid = identifier("proposal")
                lessons = self.all_lessons(state)
                lids = {lid for e in entries for lid in e["lesson_ids"]}
                prior_lids = {lid for e in state["active"][scope] for lid in e["lesson_ids"]}
                versions = {lid: version for e in entries for lid, version in e["lesson_versions"].items()
                            if not any(old["id"] == e["id"] and old == e for old in state["active"][scope])}
                proposal = {"id": pid, "scope": scope, "status": "pending", "created_at": now(),
                            "base_hash": job["base_hash"], "entries": entries, "reason": result["reason"],
                            "generalization": result.get("generalization"), "lesson_versions": versions,
                            "assessment_versions": assessment_versions,
                            "fingerprint": fingerprint, "input_hash": job["input_hash"]}
                if job.get("draft_request"):
                    proposal.update(draft_request=job["draft_request"], revision_of=result["revision_of"],
                                    requested_by=result["requested_by"], deferred_entry_ids=result["deferred_entry_ids"])
                proposal["proposal_hash"] = digest(proposal)
                for previous in state["proposals"].values():
                    replace = previous["id"] in supersede_ids if supersede_ids is not None else not previous.get("draft_request")
                    if previous["scope"] == scope and previous["status"] == "pending" and replace:
                        previous["status"] = "superseded"
                        previous["superseded_by"] = pid
                state["proposals"][pid] = proposal
                writes[f"review/proposals/{pid}.json"] = json_text(proposal)
                before = self.read(self.target(state, scope))
                diff = "".join(difflib.unified_diff(before.splitlines(True), content.splitlines(True), fromfile="active", tofile="proposed"))
                evidence = ""
                for lid in sorted(lids | prior_lids):
                    version = next((e["lesson_versions"][lid] for e in entries + state["active"][scope] if lid in e["lesson_ids"]), None)
                    archived = writes.get(f".aggregator/lesson-revisions/{version}.json") or self.read(f".aggregator/lesson-revisions/{version}.json")
                    lesson = json.loads(archived) if archived else lessons.get(lid)
                    if lesson:
                        evidence += f"\n### {lid}\n\n{lesson['statement']}\n\nApplies when: {lesson['condition']}\n"
                        for ev in lesson["evidence"]:
                            evidence += f"\n> {ev['quote']}\n\n[Preserved source {ev['session_id']}](../../.aggregator/sources/{ev['source_hash']}.md)\n"
                review = f"# {pid}\n\nScope: `{scope}`\n\nReview hash: `{proposal['proposal_hash']}`\n\n{result['reason']}\n\n{result.get('generalization', '')}\n\n```diff\n{diff}```\n\n## Supporting evidence\n{evidence}"
                writes[f"review/proposals/{pid}.md"] = review
                state.setdefault("review_presentations", {})[pid] = digest(review)
        if scope == "global":
            state["last_global_input"] = job["input_hash"]
            state["last_global_date"] = datetime.now(ZONE).date().isoformat()
            state["last_global_assessments"] = digest(self.scope_input(state, "global")["assessments"])
        else:
            # Calculate before inserting this run's proposal into the next input snapshot.
            state["project_inputs"][scope] = digest(self.scope_input(state, scope))

    def revise(self, pid, expected_hash, selection, user_reference, reason):
        """Create a review-only draft; unchecked entries and active guidance survive."""
        if not user_reference.strip() or not reason.strip():
            raise ValueError("A revision request reference and reason are required")
        if not isinstance(selection, dict) or set(selection) - {"selected_entry_ids", "edits"}:
            raise ValueError("Selection must contain selected_entry_ids and optional edits")
        ids, edits = selection.get("selected_entry_ids"), selection.get("edits", {})
        if not isinstance(ids, list) or not ids or any(not isinstance(x, str) for x in ids) or len(ids) != len(set(ids)) or not isinstance(edits, dict):
            raise ValueError("Select unique entry IDs and provide edits as an object")
        if mask(json_text(selection)) != json_text(selection) or mask(reason) != reason:
            raise ValueError("Redact credentials and URL queries before preparing a revision")
        key = digest({"source": pid, "hash": expected_hash, "selection": selection,
                      "requested_by": user_reference, "reason": reason})
        with self.locked() as state:
            existing = next((p for p in state["proposals"].values() if p.get("draft_request") == key and p["status"] == "pending"), None)
            if existing:
                return {"status": "pending", "proposal_id": existing["id"], "proposal_hash": existing["proposal_hash"], "already_prepared": True}
            source = state["proposals"].get(pid)
            if not source or source["status"] != "pending" or source["proposal_hash"] != expected_hash:
                raise ValueError("Source proposal is not pending or its reviewed hash changed")
            archived = json.loads(self.read(f"review/proposals/{pid}.json"))
            archive_hash = archived.pop("proposal_hash")
            if archive_hash != expected_hash or digest(archived) != expected_hash:
                raise ValueError("Immutable source proposal was modified")
            presentation = state.get("review_presentations", {}).get(pid)
            if presentation and digest(self.read(f"review/proposals/{pid}.md")) != presentation:
                raise ValueError("Source review presentation was modified")
            scope = source["scope"]
            self.check_published(state, scope)
            available = {e["id"]: e for e in source["entries"]}
            if set(ids) - set(available) or set(edits) - set(ids):
                raise ValueError("Selections and edits must reference selected source entries")
            replaced = [original["id"] for eid in ids for original in available[eid].get("replaces", [])]
            if len(replaced) != len(set(replaced)) or set(replaced) & set(ids):
                raise ValueError("Select a merge or its originals, not overlapping replacements")
            entries = {e["id"]: dict(e) for e in state["active"][scope]}
            for eid in ids:
                for original in available[eid].get("replaces", []):
                    if original["id"] in entries:
                        if entry_snapshot(entries[original["id"]]) != original:
                            raise ValueError("Merge original changed; refresh the selection")
                        del entries[original["id"]]
                entries[eid] = dict(available[eid])
                if eid in edits:
                    entries[eid]["text"] = edits[eid]
            deferred = [eid for eid in available if eid not in ids]
            job = {"scope": scope, "stage": "global" if scope == "global" else "project",
                   "input_hash": digest(self.scope_input(state, scope)), "base_hash": state["published"][scope], "draft_request": key}
            result = {"entries": list(entries.values()), "reason": reason, "generalization": source.get("generalization"),
                      "revision_of": pid, "requested_by": user_reference, "deferred_entry_ids": deferred}
            replace_parent = not deferred and not (set(entries) - set(available))
            previous = set(state["proposals"])
            writes = {}
            self.record_scope(state, job, result, writes, supersede_ids={pid} if replace_parent else set())
            created = set(state["proposals"]) - previous
            if not created:
                raise ValueError("Selection requires no new draft; review the current active guidance")
            new_id = created.pop()
            writes["review/inbox.md"] = self.inbox(state)
            self.commit(state, writes)
            return {"status": "pending", "proposal_id": new_id, "proposal_hash": state["proposals"][new_id]["proposal_hash"],
                    "parent_kept_pending": not replace_parent, "deferred_entry_ids": deferred}

    def merge(self, scope, spec, user_reference, reason):
        """Propose a replacement, never publish it or delete session lessons."""
        if not user_reference.strip() or not reason.strip() or not isinstance(spec, dict):
            raise ValueError("Merge specification, request reference, and reason are required")
        if set(spec) - {"base_hash", "replace_entries", "text", "conditions", "proposal_id", "proposal_hash"}:
            raise ValueError("Unknown merge specification field")
        originals = spec.get("replace_entries")
        if not isinstance(originals, list) or len(originals) < 2:
            raise ValueError("Select at least two original entry snapshots")
        if mask(json_text(spec)) != json_text(spec) or mask(reason) != reason:
            raise ValueError("Redact credentials and URL queries before proposing a merge")
        with self.locked() as state:
            self.check_published(state, scope)
            key = digest({"merge": spec, "scope": scope, "requested_by": user_reference, "reason": reason})
            prior = next((p for p in state["proposals"].values() if p.get("draft_request") == key and p["status"] == "pending"), None)
            if prior:
                return {"status": "pending", "proposal_id": prior["id"], "proposal_hash": prior["proposal_hash"], "already_prepared": True}
            if spec.get("base_hash") != state["published"][scope]:
                raise ValueError("Active KB changed; refresh the merge originals")
            source_id = spec.get("proposal_id")
            available = {e["id"]: e for e in state["active"][scope]}
            source = None
            if source_id:
                source = state["proposals"].get(source_id)
                if not source or source["status"] != "pending" or source["scope"] != scope or source["proposal_hash"] != spec.get("proposal_hash"):
                    raise ValueError("Merge source proposal is not current in this scope")
                archive = json.loads(self.read(f"review/proposals/{source_id}.json"))
                recorded_hash = archive.pop("proposal_hash")
                if recorded_hash != source["proposal_hash"] or digest(archive) != recorded_hash:
                    raise ValueError("Merge source proposal was modified")
                if digest(self.read(f"review/proposals/{source_id}.md")) != state["review_presentations"].get(source_id):
                    raise ValueError("Merge source review was modified")
                for entry in source["entries"]:
                    available.setdefault(entry["id"], entry)
            snapshots = [entry_snapshot(entry) for entry in originals]
            selected = [entry["id"] for entry in snapshots]
            if len(set(selected)) != len(selected):
                raise ValueError("Merge originals must be distinct")
            for original in snapshots:
                if original["id"] not in available or entry_snapshot(available[original["id"]]) != original:
                    raise ValueError("Merge original snapshot changed or is outside this scope")
            versions = {}
            for original in snapshots:
                for lid, version in original["lesson_versions"].items():
                    if lid in versions and versions[lid] != version:
                        raise ValueError("Merge originals disagree about a lesson version")
                    versions[lid] = version
            merged = {"id": identifier("entry"), "text": spec.get("text"), "lesson_ids": sorted(versions), "lesson_versions": versions,
                      "replaces": snapshots, "merge_conditions": spec.get("conditions")}
            entries = [entry for entry in state["active"][scope] if entry["id"] not in selected] + [merged]
            job = {"scope": scope, "input_hash": digest(self.scope_input(state, scope)), "base_hash": state["published"][scope], "draft_request": key}
            result = {"entries": entries, "reason": reason, "generalization": spec.get("conditions") if scope == "global" else None,
                      "revision_of": source_id, "requested_by": user_reference,
                      "deferred_entry_ids": [e["id"] for e in source["entries"] if e["id"] not in selected] if source else []}
            previous = set(state["proposals"]); writes = {}
            self.record_scope(state, job, result, writes, supersede_ids=set())
            created = set(state["proposals"]) - previous
            if not created:
                raise ValueError("No distinct merge proposal was produced")
            pid = created.pop(); writes["review/inbox.md"] = self.inbox(state)
            self.commit(state, writes)
            return {"status": "pending", "proposal_id": pid, "proposal_hash": state["proposals"][pid]["proposal_hash"],
                    "merged_entry_id": merged["id"], "replaces_entry_ids": selected}

    def inbox(self, state):
        pending = [p for p in state["proposals"].values() if p["status"] == "pending"]
        text = "# Review inbox\n\nOnly an explicit user decision can publish or invalidate guidance.\n"
        active_lids = {lid for entries in state["active"].values() for e in entries for lid in e["lesson_ids"]}
        contradictions = []
        for session in state["sessions"].values():
            content = self.read(session["folder"] + "/assessments.md")
            for block in re.findall(r"```json\n(.*?)\n```", content, re.S):
                event = json.loads(block)
                if event.get("status") == "pending" and event.get("decision") in ("disputed", "invalidated", "superseded") and event.get("lesson_id") in active_lids:
                    contradictions.append((session, event))
        if contradictions:
            text += "\n## Priority: disputed active guidance\n\nThese assessments have NOT suspended guidance. Review a corresponding withdrawal/replacement proposal.\n"
            for session, event in contradictions:
                text += f"\n- `{event['lesson_id']}` — {event['reason']} — [assessment](../{session['folder']}/assessments.md)\n"
        text += "\n## Proposed changes\n"
        for p in pending:
            stale = p["base_hash"] != state["published"][p["scope"]]
            text += f"\n- [{p['id']}](proposals/{p['id']}.md) — `{p['scope']}`" + (" — **stale; regenerate before approval**" if stale else "") + f"\n  {p['reason']}\n"
        if not pending:
            text += "\nNo pending proposals.\n"
        return text

    def record(self, rid, jid, result):
        if mask(json_text(result)) != json_text(result):
            raise ValueError("Analysis contains credentials or unsanitized URLs; redact before recording")
        with self.locked() as state:
            run = self.current_run(state, rid)
            if jid not in run["jobs"]:
                raise ValueError("Unknown job")
            if run["jobs"][jid]["status"] == "recorded":
                return {"status": "already_recorded", "job_id": jid}
            job = json.loads(self.read(f".aggregator/runs/{rid}/{jid}.json"))
            writes = {}
            if job["stage"] == "session":
                self.record_session(state, job, result, writes)
            else:
                self.record_scope(state, job, result, writes)
            run["jobs"][jid]["status"] = "recorded"
            writes[f".aggregator/runs/{rid}/{jid}.result.json"] = json_text(result)
            writes["review/inbox.md"] = self.inbox(state)
            self.commit(state, writes)
            return {"status": "recorded", "job_id": jid}

    def finish(self, rid, abandon=False):
        with self.locked() as state:
            run = self.current_run(state, rid)
            pending = [jid for jid, j in run["jobs"].items() if j["status"] == "pending"]
            if pending and not abandon:
                raise ValueError("Pending jobs remain; record them or explicitly abandon the run")
            report = dict(run, finished_at=now(), status="abandoned" if abandon else "completed",
                          backlog=state.get("backlog", 0), pending_proposals=sum(p["status"] == "pending" for p in state["proposals"].values()),
                          source_errors=state.get("source_errors", []))
            state["run"] = None
            self.commit(state, {f".aggregator/runs/{rid}/report.json": json_text(report), "review/inbox.md": self.inbox(state)})
            return report

    def decision(self, pid, action, expected_hash, user_reference, reason):
        if not user_reference.strip() or not reason.strip():
            raise ValueError("An explicit user decision reference and reason are required")
        with self.locked() as state:
            proposal = state["proposals"].get(pid)
            if not proposal or proposal["status"] != "pending":
                raise ValueError("Proposal is missing or already decided")
            if proposal["proposal_hash"] != expected_hash:
                raise ValueError("Approval does not match the reviewed proposal hash")
            archived = json.loads(self.read(f"review/proposals/{pid}.json"))
            archived_hash = archived.pop("proposal_hash")
            if archived_hash != expected_hash or digest(archived) != expected_hash:
                raise ValueError("Immutable proposal record was modified")
            presentation = state.get("review_presentations", {}).get(pid)
            if presentation and digest(self.read(f"review/proposals/{pid}.md")) != presentation:
                raise ValueError("Review presentation was modified; regenerate before deciding")
            scope = proposal["scope"]
            self.check_published(state, scope)
            writes = {}
            if action == "approve":
                if state["published"][scope] != proposal["base_hash"]:
                    raise ValueError("Stale approval: active KB changed; regenerate and review")
                for sid, expected in proposal.get("assessment_versions", {}).items():
                    if digest(self.read(state["sessions"][sid]["folder"] + "/assessments.md")) != expected:
                        raise ValueError("Stale approval: lesson assessments changed")
                lessons = self.all_lessons(state)
                for lid, version in proposal["lesson_versions"].items():
                    versions = {v for pid in state["projects"] for e in state["active"][pid] for l, v in e["lesson_versions"].items() if l == lid} if scope == "global" else {lessons.get(lid, {}).get("version")}
                    if lid not in lessons or version not in versions:
                        raise ValueError("Stale approval: supporting lesson changed")
                entries = self.validate_entries(state, scope, proposal["entries"])
                target = self.target(state, scope)
                before = self.read(target)
                writes[f".aggregator/revisions/{scope}/{digest(before)}.md"] = before
                writes[f".aggregator/revisions/{scope}/{digest(before)}.json"] = json_text(state["active"][scope])
                old_lids = {lid for e in state["active"][scope] for lid in e["lesson_ids"]}
                new_lids = {lid for e in entries for lid in e["lesson_ids"]}
                writes[target] = self.render_kb(entries, scope)
                state["active"][scope] = entries
                state["published"][scope] = digest(writes[target])
                for sid, session in state["sessions"].items():
                    session_lids = {l["id"] for l in session["lessons"]}
                    for lid in sorted(session_lids & (old_lids | new_lids)):
                        event = {"id": identifier("assessment"), "at": now(), "lesson_id": lid,
                                 "decision": "promoted" if lid in new_lids else "withdrawn", "status": "approved",
                                 "scope": scope, "proposal_id": pid, "user_reference": user_reference, "reason": reason}
                        self.append_assessment(state, writes, sid, event)
            proposal["status"] = "approved" if action == "approve" else "rejected"
            proposal["review_reason"] = reason
            state["decisions"].append({"proposal_id": pid, "action": action, "proposal_hash": expected_hash,
                                       "at": now(), "user_reference": user_reference, "reason": reason})
            writes["review/inbox.md"] = self.inbox(state)
            self.commit(state, writes)
            return {"status": proposal["status"], "proposal_id": pid}

    def rollback(self, scope, revision, user_reference, reason):
        with self.locked() as state:
            self.check_published(state, scope)
            if not re.fullmatch(r"[a-f0-9]{64}", revision):
                raise ValueError("Invalid revision")
            content = self.read(f".aggregator/revisions/{scope}/{revision}.json")
            if not content:
                raise ValueError("Unknown revision")
            entries = self.validate_entries(state, scope, json.loads(content))
            proposal = {"id": identifier("proposal"), "scope": scope, "status": "pending", "created_at": now(),
                        "base_hash": state["published"][scope], "entries": entries, "reason": "Rollback: " + reason,
                        "lesson_versions": {lid: l["version"] for lid, l in self.all_lessons(state).items() if any(lid in e["lesson_ids"] for e in entries)},
                        "fingerprint": identifier("rollback"), "input_hash": revision}
            proposal["proposal_hash"] = digest(proposal)
            state["proposals"][proposal["id"]] = proposal
            self.commit(state, {f"review/proposals/{proposal['id']}.json": json_text(proposal)})
        return self.decision(proposal["id"], "approve", proposal["proposal_hash"], user_reference, reason)

    def context(self, cwd, query=None):
        with self.locked(readonly=True) as state:
            cwd = Path(cwd).expanduser().resolve()
            matches = [(len(Path(p).parts), pid) for pid, project in state["projects"].items()
                       for p in project["paths"] if cwd.is_relative_to(Path(p))]
            scopes = ["global"] + ([max(matches)[1]] if matches else [])
            text = "Learned advisory context. Explicit current instructions and project rules take precedence.\n"
            for scope in scopes:
                self.check_published(state, scope)
                entries = state["active"][scope]
                if query:
                    terms = normalized(query).split()
                    entries = [e for e in entries if any(t in normalized(e["text"]) for t in terms)]
                text += f"\nScope: {scope}\nSource: {self.path(self.target(state, scope))}\n" + self.render_kb(entries, scope)
            return text

    def status(self, recover=False):
        with self.locked(readonly=not recover) as state:
            return {"root": str(self.root), "projects": len(state["projects"]), "sessions": len(state["sessions"]),
                    "lessons": len(self.all_lessons(state)), "pending_proposals": sum(p["status"] == "pending" for p in state["proposals"].values()),
                    "approved_proposals": sum(p["status"] == "approved" for p in state["proposals"].values()),
                    "backlog": state.get("backlog", 0), "run": state.get("run"), "last_global_date": state["last_global_date"],
                    "source_errors": state.get("source_errors", [])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init"); p.add_argument("--source", required=True)
    p = sub.add_parser("begin"); p.add_argument("--inventory", required=True)
    p = sub.add_parser("prepare"); p.add_argument("--run-id", required=True); p.add_argument("--stage", choices=["session", "project", "global"], required=True)
    p = sub.add_parser("record"); p.add_argument("--run-id", required=True); p.add_argument("--job-id", required=True); p.add_argument("--result", required=True)
    p = sub.add_parser("evidence"); p.add_argument("--session-id", required=True); p.add_argument("--input", required=True)
    p = sub.add_parser("revise"); p.add_argument("proposal_id"); p.add_argument("--hash", required=True); p.add_argument("--selection", required=True); p.add_argument("--user-reference", required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("merge"); p.add_argument("scope"); p.add_argument("--spec", required=True); p.add_argument("--user-reference", required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("finish"); p.add_argument("--run-id", required=True); p.add_argument("--abandon", action="store_true")
    for command in ("approve", "reject"):
        p = sub.add_parser(command); p.add_argument("proposal_id"); p.add_argument("--hash", required=True); p.add_argument("--user-reference", required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("rollback"); p.add_argument("scope"); p.add_argument("revision"); p.add_argument("--user-reference", required=True); p.add_argument("--reason", required=True)
    p = sub.add_parser("context"); p.add_argument("--cwd", required=True); p.add_argument("--query")
    p = sub.add_parser("status"); p.add_argument("--recover", action="store_true")
    sub.add_parser("inbox")
    args = parser.parse_args()
    kb = KB(args.root)
    try:
        if args.command == "init": result = kb.init(args.source)
        elif args.command == "begin": result = kb.begin(json.loads(Path(args.inventory).read_text()))
        elif args.command == "prepare": result = kb.prepare(args.run_id, args.stage)
        elif args.command == "record": result = kb.record(args.run_id, args.job_id, json.loads(Path(args.result).read_text()))
        elif args.command == "evidence": result = kb.excerpt(args.session_id, json.loads(Path(args.input).read_text()))
        elif args.command == "revise": result = kb.revise(args.proposal_id, args.hash, json.loads(Path(args.selection).read_text()), args.user_reference, args.reason)
        elif args.command == "merge": result = kb.merge(args.scope, json.loads(Path(args.spec).read_text()), args.user_reference, args.reason)
        elif args.command == "finish": result = kb.finish(args.run_id, args.abandon)
        elif args.command in ("approve", "reject"): result = kb.decision(args.proposal_id, args.command, args.hash, args.user_reference, args.reason)
        elif args.command == "rollback": result = kb.rollback(args.scope, args.revision, args.user_reference, args.reason)
        elif args.command == "context": result = kb.context(args.cwd, args.query)
        elif args.command == "inbox":
            with kb.locked(readonly=True) as state: result = kb.inbox(state)
        else: result = kb.status(args.recover)
        print(result if isinstance(result, str) else json_text(result), end="\n" if isinstance(result, str) else "")
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json_text({"error": mask(str(exc))}), file=sys.stderr, end="")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
