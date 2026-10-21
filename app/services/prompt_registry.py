"""Prompt Registry — versioned, auditable prompt artifacts.

Prompts are data, not code: each agent's system prompt lives in
``prompts/<name>/v<N>.yaml`` (git-reviewed seed files), loaded and validated
at startup. Mutable lifecycle state — runtime-created candidates, status
transitions — is persisted as a SQLite overlay on top of the YAML seeds,
so promotions survive restarts while the files remain the source of truth
for template content.

Invariants enforced at load (violations fail fast — the app never silently
falls back to stale constants):

- declared ``variables`` == ``{placeholders}`` found in the template;
- versions are unique and contiguous per name (1..N, no gaps);
- exactly one ``promoted`` version per name;
- a committed ``vN.yaml`` meeting a ``source='runtime'`` row must carry
  the same template hash to be adopted as the new source — a different
  hash aborts startup instead of silently shadowing either text.

The registry is thread-safe for the low write volume it sees (a
``threading.Lock`` guards mutations and audit-chain appends).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
import string
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, Field

from app.services.audit import GENESIS

DEFAULT_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

_STATUSES = {"draft", "candidate", "promoted", "retired", "rolled_back"}


class PromptStatus(StrEnum):
    """Lifecycle status of a prompt version."""

    DRAFT = "draft"
    CANDIDATE = "candidate"
    PROMOTED = "promoted"
    RETIRED = "retired"
    ROLLED_BACK = "rolled_back"


class PromptRegistryError(Exception):
    """Any registry violation — treated as fatal at startup."""


class PromptVersion(BaseModel):
    """One immutable prompt version (content) plus its mutable status."""

    name: str
    version: int = Field(ge=1)
    status: PromptStatus = PromptStatus.DRAFT
    author: str = ""
    created_at: datetime
    target_model: str = ""
    variables: list[str] = Field(default_factory=list)
    changelog: str = ""
    template: str
    sha256: str = ""
    source: str = "yaml"  # "yaml" seed | "runtime" DB-created


class RenderedPrompt(BaseModel):
    """A fully-rendered prompt plus its provenance fingerprint."""

    text: str
    name: str
    version: int
    sha256: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_versions (
    name              TEXT NOT NULL,
    version           INTEGER NOT NULL,
    status            TEXT NOT NULL,
    author            TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    target_model      TEXT NOT NULL DEFAULT '',
    variables         TEXT NOT NULL DEFAULT '[]',
    changelog         TEXT NOT NULL DEFAULT '',
    template          TEXT NOT NULL,
    sha256            TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT 'yaml',
    status_overridden INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS prompt_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    action       TEXT NOT NULL,
    actor        TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL,
    sig          TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompt_ab_runs (
    run_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    version      INTEGER NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL,
    result_json  TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);
"""


def _placeholders(template: str) -> set[str]:
    """Field names referenced by ``str.format`` placeholders in a template."""
    found: set[str] = set()
    for _lit, field, _spec, _conv in string.Formatter().parse(template):
        if field is not None:
            found.add(field.split(".")[0].split("[")[0])
    return found


def _sha(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def _event_canonical(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


class PromptRegistry:
    """Read API + lifecycle mutations over YAML seeds and a SQLite overlay."""

    def __init__(
        self,
        versions: dict[tuple[str, int], PromptVersion],
        conn: sqlite3.Connection | None,
        *,
        promoted_overrides: dict[str, int] | None = None,
        yaml_versions: dict[tuple[str, int], PromptVersion] | None = None,
        signing_key: str | None = None,
        previous_keys: Sequence[str] = (),
    ) -> None:
        self._versions = versions
        self._conn = conn
        self._signing_key = signing_key
        # Verification keyring {key_id: key}: current key first, then
        # retired keys — signatures minted pre-rotation stay verifiable.
        from app.services.audit import key_id_for

        self._keyring: dict[str, str] = {}
        if signing_key:
            self._keyring[key_id_for(signing_key)] = signing_key
        for key in previous_keys:
            self._keyring.setdefault(key_id_for(key), key)
        self._lock = threading.Lock()
        self._promoted_overrides = dict(promoted_overrides or {})
        # Pristine YAML seeds kept for cross-process refreshes: a second
        # process may promote/create versions, so reads re-merge the DB
        # overlay whenever ``prompt_events`` grew since the last check.
        self._yaml_versions = yaml_versions if yaml_versions is not None else versions
        self._revision = self._db_revision()

    # ── loading ──────────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        prompts_dir: Path | str = DEFAULT_PROMPTS_DIR,
        db_path: str | None = None,
        signing_key: str | None = None,
        previous_keys: Sequence[str] = (),
    ) -> PromptRegistry:
        """Load YAML seeds (+ SQLite overlay when ``db_path`` is given).

        Raises ``PromptRegistryError`` on any invalid artifact — callers let
        it propagate so the process fails fast at startup.
        """
        prompts_dir = Path(prompts_dir)
        if not prompts_dir.is_dir():
            raise PromptRegistryError(f"prompts directory missing: {prompts_dir}")
        versions = cls._load_yaml(prompts_dir)

        conn: sqlite3.Connection | None = None
        yaml_versions = versions
        if db_path is not None:
            conn = cls._open_db(db_path)
            cls._sync_yaml(conn, versions)
            versions = cls._merge_db(conn, versions)

        cls._validate(versions)
        return cls(
            versions,
            conn,
            yaml_versions=yaml_versions,
            signing_key=signing_key,
            previous_keys=previous_keys,
        )

    @staticmethod
    def _load_yaml(prompts_dir: Path) -> dict[tuple[str, int], PromptVersion]:
        versions: dict[tuple[str, int], PromptVersion] = {}
        for path in sorted(prompts_dir.glob("*/v*.yaml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise PromptRegistryError(f"invalid YAML in {path}: {exc}") from exc
            if not isinstance(raw, dict):
                raise PromptRegistryError(f"{path}: expected a mapping at top level")
            try:
                pv = PromptVersion.model_validate(raw)
            except Exception as exc:
                raise PromptRegistryError(f"{path}: {exc}") from exc
            if pv.name != path.parent.name:
                raise PromptRegistryError(
                    f"{path}: name '{pv.name}' != directory '{path.parent.name}'"
                )
            if f"v{pv.version}.yaml" != path.name:
                raise PromptRegistryError(
                    f"{path}: filename does not match version {pv.version}"
                )
            if pv.status not in _STATUSES:
                raise PromptRegistryError(f"{path}: bad status {pv.status!r}")
            pv.sha256 = _sha(pv.template)
            pv.source = "yaml"
            key = (pv.name, pv.version)
            if key in versions:
                raise PromptRegistryError(f"duplicate {pv.name} v{pv.version}")
            versions[key] = pv
        if not versions:
            raise PromptRegistryError(f"no prompt artifacts under {prompts_dir}")
        return versions

    @staticmethod
    def _open_db(db_path: str) -> sqlite3.Connection:
        from app.services.sqlite_util import connect_sync

        conn = connect_sync(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        # Guarded migration: older DBs lack the per-event HMAC column.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(prompt_events)")}
        if "sig" not in cols:
            conn.execute(
                "ALTER TABLE prompt_events ADD COLUMN sig TEXT NOT NULL DEFAULT ''"
            )
            conn.commit()
        return conn

    @staticmethod
    def _sync_yaml(
        conn: sqlite3.Connection, versions: dict[tuple[str, int], PromptVersion]
    ) -> None:
        """Upsert seed content; runtime status overrides always win.

        Collision rule when a committed ``vN.yaml`` meets an existing
        ``source='runtime'`` row: identical template hashes simply adopt
        the file as the new source; a *different* hash fails fast — the
        operator must resolve which body of prompt text is real instead
        of silently shadowing one.
        """
        for pv in versions.values():
            row = conn.execute(
                "SELECT sha256, source FROM prompt_versions"
                " WHERE name=? AND version=?",
                (pv.name, pv.version),
            ).fetchone()
            if row is not None and row["source"] == "runtime":
                existing = row["sha256"]
                if existing != pv.sha256:
                    raise PromptRegistryError(
                        f"{pv.name} v{pv.version}: YAML seed conflicts with "
                        "the runtime-created version (different template "
                        "hash). Delete the DB row or rename the file — "
                        "refusing to pick one silently."
                    )
            conn.execute(
                "INSERT INTO prompt_versions (name, version, status, author,"
                " created_at, target_model, variables, changelog, template,"
                " sha256, source, status_overridden)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,'yaml',0)"
                " ON CONFLICT(name, version) DO UPDATE SET"
                "   author=excluded.author, created_at=excluded.created_at,"
                "   target_model=excluded.target_model,"
                "   variables=excluded.variables, changelog=excluded.changelog,"
                "   template=excluded.template, sha256=excluded.sha256,"
                "   source='yaml',"
                "   status=CASE WHEN prompt_versions.status_overridden=1"
                "     THEN prompt_versions.status ELSE excluded.status END",
                (
                    pv.name, pv.version, pv.status.value, pv.author,
                    pv.created_at.isoformat(), pv.target_model,
                    json.dumps(pv.variables), pv.changelog, pv.template,
                    pv.sha256,
                ),
            )
        conn.commit()

    @staticmethod
    def _merge_db(
        conn: sqlite3.Connection, yaml_versions: dict[tuple[str, int], PromptVersion]
    ) -> dict[tuple[str, int], PromptVersion]:
        """Effective view = YAML content, DB-overlaid status, + runtime rows."""
        merged = dict(yaml_versions)
        rows = conn.execute(
            "SELECT * FROM prompt_versions ORDER BY name, version"
        ).fetchall()
        for row in rows:
            key = (row["name"], row["version"])
            db_status = PromptStatus(row["status"])
            if row["source"] == "yaml":
                base = merged.get(key)
                if base is not None:
                    base.status = db_status
                    continue
            # Runtime-created version (or a DB row without a YAML seed):
            # the overlay owns it.
            try:
                merged[key] = PromptVersion(
                    name=row["name"],
                    version=row["version"],
                    status=db_status,
                    author=row["author"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    target_model=row["target_model"],
                    variables=json.loads(row["variables"]),
                    changelog=row["changelog"],
                    template=row["template"],
                    sha256=row["sha256"] or _sha(row["template"]),
                    source="runtime",
                )
            except Exception as exc:
                raise PromptRegistryError(
                    f"prompt_versions row {key} invalid: {exc}"
                ) from exc
        return merged

    @staticmethod
    def _validate(versions: dict[tuple[str, int], PromptVersion]) -> None:
        by_name: dict[str, list[PromptVersion]] = {}
        for pv in versions.values():
            declared = set(pv.variables)
            found = _placeholders(pv.template)
            if declared != found:
                raise PromptRegistryError(
                    f"{pv.name} v{pv.version}: declared variables {sorted(declared)}"
                    f" != template placeholders {sorted(found)}"
                )
            by_name.setdefault(pv.name, []).append(pv)
        for name, pvs in by_name.items():
            nums = sorted(p.version for p in pvs)
            if nums != list(range(1, len(nums) + 1)):
                raise PromptRegistryError(
                    f"{name}: non-contiguous versions {nums}"
                )
            promoted = [p for p in pvs if p.status == PromptStatus.PROMOTED]
            if len(promoted) != 1:
                raise PromptRegistryError(
                    f"{name}: expected exactly one promoted version,"
                    f" found {len(promoted)}"
                )

    # ── cross-process freshness ──────────────────────────────────────────

    def _db_revision(self) -> int:
        """Monotonic registry revision — MAX(seq) of the lifecycle chain."""
        if self._conn is None:
            return -1
        row = self._conn.execute(
            "SELECT MAX(seq) AS s FROM prompt_events"
        ).fetchone()
        return int(row["s"] or 0)

    def _maybe_refresh(self) -> None:
        """Re-merge the DB overlay when another process appended events.

        Reads the revision first, then merges — if a write lands between
        the two the stored revision stays stale and the next read merges
        again (self-healing, never serves older data than the check).
        """
        if self._conn is None:
            return
        rev = self._db_revision()
        if rev == self._revision:
            return
        with self._lock:
            self._versions = self._merge_db(self._conn, dict(self._yaml_versions))
            self._revision = rev

    # ── read API ─────────────────────────────────────────────────────────

    def names(self) -> list[str]:
        self._maybe_refresh()
        return sorted({n for n, _v in self._versions})

    def list_versions(self, name: str | None = None) -> list[PromptVersion]:
        self._maybe_refresh()
        pvs = [p for (n, _v), p in self._versions.items() if name is None or n == name]
        return sorted(pvs, key=lambda p: (p.name, p.version))

    def get(self, name: str, version: int | None = None) -> PromptVersion:
        self._maybe_refresh()
        if version is None:
            version = self._promoted_overrides.get(name)
        if version is None:
            promoted = [
                p for (n, _v), p in self._versions.items()
                if n == name and p.status == PromptStatus.PROMOTED
            ]
            if not promoted:
                raise PromptRegistryError(f"{name}: no promoted version")
            return promoted[0]
        pv = self._versions.get((name, version))
        if pv is None:
            raise PromptRegistryError(f"{name} v{version}: not found")
        return pv

    def export_yaml(self, name: str, version: int) -> str:
        """Render a stored version as a review-ready ``vN.yaml`` document.

        The field order and shape match the git seeds, so the output of
        ``scripts/export_prompt.py`` can be committed and reloaded
        byte-for-byte semantically. Lifecycle nuance: ``promoted`` exports
        as ``promoted``; every other runtime state exports as
        ``candidate`` — a file under review, never silently shipped.
        """
        pv = self.get(name, version)
        created = pv.created_at.isoformat().replace("+00:00", "Z")
        doc = {
            "name": pv.name,
            "version": pv.version,
            "status": (
                PromptStatus.PROMOTED.value
                if pv.status == PromptStatus.PROMOTED
                else PromptStatus.CANDIDATE.value
            ),
            "author": pv.author,
            "created_at": created,
            "target_model": pv.target_model,
            "variables": list(pv.variables),
            "changelog": pv.changelog,
            "template": pv.template,
        }
        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)

    def render(
        self, name: str, *, version: int | None = None, **variables: Any
    ) -> RenderedPrompt:
        pv = self.get(name, version)
        missing = set(pv.variables) - set(variables)
        if missing:
            raise PromptRegistryError(
                f"{name} v{pv.version}: missing variables {sorted(missing)}"
            )
        try:
            text = pv.template.format(**variables)
        except (KeyError, IndexError, ValueError) as exc:
            raise PromptRegistryError(
                f"{name} v{pv.version}: render failed: {exc}"
            ) from exc
        return RenderedPrompt(
            text=text, name=name, version=pv.version, sha256=pv.sha256
        )

    def diff(self, name: str, a: int, b: int) -> str:
        va, vb = self.get(name, a), self.get(name, b)
        lines = difflib.unified_diff(
            va.template.splitlines(keepends=True),
            vb.template.splitlines(keepends=True),
            fromfile=f"{name} v{a}",
            tofile=f"{name} v{b}",
        )
        return "".join(lines)

    def with_overrides(self, overrides: dict[str, int]) -> PromptRegistry:
        """Read-only view resolving ``get``/``render`` to pinned versions.

        Used by A/B evaluation: the registry data is shared, only the
        promoted-version resolution changes. Mutations are rejected.
        """
        return PromptRegistry(
            self._versions, None, promoted_overrides=overrides
        )

    # ── mutations (DB-backed) ────────────────────────────────────────────

    def _require_db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise PromptRegistryError("registry is read-only (no database)")
        return self._conn

    def _persist(self, pv: PromptVersion, *, status_overridden: bool) -> None:
        conn = self._require_db()
        conn.execute(
            "INSERT INTO prompt_versions (name, version, status, author,"
            " created_at, target_model, variables, changelog, template,"
            " sha256, source, status_overridden)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(name, version) DO UPDATE SET"
            "   status=excluded.status,"
            "   status_overridden=excluded.status_overridden",
            (
                pv.name, pv.version, pv.status.value, pv.author,
                pv.created_at.isoformat(), pv.target_model,
                json.dumps(pv.variables), pv.changelog, pv.template,
                pv.sha256, pv.source, 1 if status_overridden else 0,
            ),
        )
        conn.commit()

    def _set_status(self, pv: PromptVersion, status: PromptStatus) -> None:
        pv.status = status
        self._persist(pv, status_overridden=True)

    def _sign(self, digest: str) -> str:
        """``key_id:HMAC`` over an event hash when a key is configured —
        the id lets verification pick the right key after a rotation."""
        if not self._signing_key:
            return ""
        import hmac

        from app.services.audit import key_id_for

        mac = hmac.new(
            self._signing_key.encode(), digest.encode(), hashlib.sha256
        ).hexdigest()
        return f"{key_id_for(self._signing_key)}:{mac}"

    def _record_event(
        self,
        *,
        name: str,
        version: int,
        action: str,
        actor: str,
        reason: str,
        metrics: dict[str, Any] | None,
    ) -> str:
        """Append to the prompt-lifecycle hash chain; returns the entry hash."""
        conn = self._require_db()
        now = datetime.now(UTC).isoformat()
        prev = conn.execute(
            "SELECT hash FROM prompt_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = str(prev["hash"]) if prev else GENESIS
        payload = {
            "name": name, "version": version, "action": action,
            "actor": actor, "reason": reason,
            "metrics": metrics or {}, "prev_hash": prev_hash,
            "created_at": now,
        }
        digest = hashlib.sha256(
            f"{prev_hash}|{_event_canonical(payload)}".encode()
        ).hexdigest()
        sig = self._sign(digest)
        conn.execute(
            "INSERT INTO prompt_events (name, version, action, actor, reason,"
            " metrics_json, prev_hash, hash, sig, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                name, version, action, actor, reason,
                json.dumps(metrics or {}, sort_keys=True),
                prev_hash, digest, sig, now,
            ),
        )
        conn.commit()
        # Own writes bump the revision so the next read doesn't re-merge.
        self._revision = self._db_revision()
        return digest

    def create_candidate(
        self, name: str, template: str, changelog: str, *, author: str = ""
    ) -> PromptVersion:
        """Register a new candidate version for ``name`` (validated first)."""
        if name not in self.names():
            raise PromptRegistryError(f"unknown prompt name: {name}")
        declared = _placeholders(template)
        existing = self.get(name)
        if declared != set(existing.variables):
            raise PromptRegistryError(
                f"candidate variables {sorted(declared)} must match the"
                f" promoted contract {sorted(existing.variables)}"
            )
        next_version = max(p.version for p in self.list_versions(name)) + 1
        pv = PromptVersion(
            name=name,
            version=next_version,
            status=PromptStatus.CANDIDATE,
            author=author,
            created_at=datetime.now(UTC),
            target_model=existing.target_model,
            variables=sorted(declared),
            changelog=changelog,
            template=template,
            sha256=_sha(template),
            source="runtime",
        )
        with self._lock:
            self._persist(pv, status_overridden=False)
            self._record_event(
                name=name, version=next_version, action="candidate",
                actor=author, reason=changelog, metrics=None,
            )
        self._versions[(name, next_version)] = pv
        return pv

    def promote(
        self,
        name: str,
        version: int,
        *,
        actor: str = "",
        reason: str = "",
        metrics: dict[str, Any] | None = None,
        override: bool = False,
    ) -> PromptVersion:
        """Promote ``version``; the previous promoted version is retired."""
        target = self.get(name, version)
        current = self.get(name)
        if target.version == current.version:
            raise PromptRegistryError(f"{name} v{version} is already promoted")
        if target.status not in (PromptStatus.CANDIDATE, PromptStatus.ROLLED_BACK):
            raise PromptRegistryError(
                f"{name} v{version}: only a candidate can be promoted"
            )
        with self._lock:
            self._set_status(current, PromptStatus.RETIRED)
            self._set_status(target, PromptStatus.PROMOTED)
            self._record_event(
                name=name, version=version,
                action="override" if override else "promote",
                actor=actor, reason=reason, metrics=metrics,
            )
        return target

    def rollback(
        self, name: str, to_version: int, *, actor: str = "", reason: str = ""
    ) -> PromptVersion:
        """Demote the current promoted to ``rolled_back``; restore ``to_version``."""
        target = self.get(name, to_version)
        current = self.get(name)
        if target.version == current.version:
            raise PromptRegistryError(f"{name} v{to_version} is already promoted")
        with self._lock:
            self._set_status(current, PromptStatus.ROLLED_BACK)
            self._set_status(target, PromptStatus.PROMOTED)
            self._record_event(
                name=name, version=to_version, action="rollback",
                actor=actor, reason=reason, metrics=None,
            )
        return target

    # ── lifecycle audit chain ────────────────────────────────────────────

    def audit_events(self) -> list[dict[str, Any]]:
        conn = self._require_db()
        rows = conn.execute(
            "SELECT * FROM prompt_events ORDER BY seq"
        ).fetchall()
        return [dict(r) for r in rows]

    def verify_audit_chain(
        self,
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Recompute the lifecycle hash chain; return (valid, root, entries).

        Each entry gains ``signature_valid``: ``True``/``False`` when the
        registry holds a signing key and a signature is stored, ``None``
        otherwise. The head entry's flag is the chain's authenticity check —
        a DB-only attacker can recompute hashes but not HMACs.
        """
        events = self.audit_events()
        prev = GENESIS
        entries: list[dict[str, Any]] = []
        valid = True
        for row in events:
            payload = {
                "name": row["name"], "version": row["version"],
                "action": row["action"], "actor": row["actor"],
                "reason": row["reason"],
                "metrics": json.loads(row["metrics_json"]),
                "prev_hash": row["prev_hash"], "created_at": row["created_at"],
            }
            digest = hashlib.sha256(
                f"{row['prev_hash']}|{_event_canonical(payload)}".encode()
            ).hexdigest()
            if row["prev_hash"] != prev or digest != row["hash"]:
                valid = False
            stored_sig = str(row.get("sig") or "")
            sig_valid: bool | None = None
            if self._keyring and stored_sig:
                import hmac

                kid, sep, sig = stored_sig.partition(":")
                if sep:
                    key = self._keyring.get(kid)
                    keys = [key] if key is not None else []
                else:
                    keys = list(self._keyring.values())
                sig_valid = any(
                    hmac.compare_digest(
                        sig,
                        hmac.new(
                            k.encode(), digest.encode(), hashlib.sha256
                        ).hexdigest(),
                    )
                    for k in keys
                )
            entries.append(
                {**row, "recomputed_hash": digest,
                 "signature_valid": sig_valid}
            )
            prev = row["hash"]
        root = prev if events else GENESIS
        return valid, root, entries

    # ── A/B run records ─────────────────────────────────────────────────

    def save_ab_run(
        self,
        run_id: str,
        *,
        name: str,
        version: int,
        workspace_id: str,
        result: dict[str, Any],
    ) -> bool:
        """Upsert an A/B run record; no-ops (returns False) without a DB."""
        conn = self._conn
        if conn is None:
            return False
        conn.execute(
            "INSERT INTO prompt_ab_runs"
            " (run_id, name, version, workspace_id, status, result_json,"
            "  created_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id) DO UPDATE SET"
            "   status=excluded.status, result_json=excluded.result_json",
            (
                run_id, name, version, workspace_id,
                str(result.get("status", "running")),
                json.dumps(result, sort_keys=True),
                str(result.get("created_at") or datetime.now(UTC).isoformat()),
            ),
        )
        conn.commit()
        return True

    def get_ab_run(
        self, run_id: str, workspace_id: str
    ) -> dict[str, Any] | None:
        """Fetch a persisted A/B run scoped to ``workspace_id``."""
        conn = self._conn
        if conn is None:
            return None
        row = conn.execute(
            "SELECT * FROM prompt_ab_runs"
            " WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = json.loads(row["result_json"])
        result.setdefault("run_id", row["run_id"])
        result.setdefault("name", row["name"])
        result.setdefault("version", row["version"])
        result.setdefault("status", row["status"])
        result.setdefault("created_at", row["created_at"])
        return result

    # ── metrics ──────────────────────────────────────────────────────────

    def publish_metrics(self) -> None:
        """Export promoted-version info as Prometheus gauges."""
        from app.core.metrics import PROMPT_VERSION_INFO

        for name in self.names():
            for pv in self.list_versions(name):
                PROMPT_VERSION_INFO.labels(name=name, version=str(pv.version)).set(
                    1 if pv.status == PromptStatus.PROMOTED else 0
                )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            self._conn.close()
