"""Fail-closed local retention for safely ended NOI Linux contests."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import time


_TID = re.compile(r"^[0-9a-f]{24}$")


class RetentionManager:
    """Remove local working copies without ever touching OJ authority data."""

    def __init__(
        self,
        store,
        *,
        artifact_root: str,
        materials_dir: str,
        collected_dir: str,
        workspace_retention_days: int = 30,
        evidence_retention_days: int = 180,
    ):
        self.store = store
        self.artifact_root = Path(artifact_root).resolve()
        self.materials_dir = Path(materials_dir).resolve()
        self.collected_dir = Path(collected_dir).resolve()
        self.workspace_days = int(workspace_retention_days)
        self.evidence_days = int(evidence_retention_days)
        if self.workspace_days <= 0 or self.evidence_days < self.workspace_days:
            raise ValueError("retention boundaries are invalid")
        if len({self.artifact_root, self.materials_dir, self.collected_dir}) != 3:
            raise ValueError("retention roots must be distinct")

    @staticmethod
    def _contest_path(root: Path, tid: str) -> Path:
        if not _TID.fullmatch(str(tid)):
            raise ValueError("retention tid is invalid")
        path = (root / str(tid)).resolve()
        if root not in path.parents:
            raise RuntimeError("retention path escapes its configured root")
        return path

    @staticmethod
    def _assert_plain_tree(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise RuntimeError("retention target is not a plain directory")
        if not path.exists():
            return
        for child in path.rglob("*"):
            if child.is_symlink():
                raise RuntimeError("retention tree contains a symbolic link")

    @classmethod
    def _remove_tree(cls, path: Path) -> None:
        cls._assert_plain_tree(path)
        if path.exists():
            shutil.rmtree(path)
        if path.exists():
            raise RuntimeError("retention target still exists after deletion")

    def _purge_workspace(self, contest: dict) -> None:
        """Delete non-approved material revisions; retain final evidence."""
        tid = str(contest["tid"])
        root = self._contest_path(self.artifact_root, tid)
        self._assert_plain_tree(root)
        if not root.exists():
            return
        revisions = self.store.artifact_revisions(tid)
        known: dict[str, Path] = {}
        for revision in revisions:
            path = Path(str(revision["root_path"])).resolve()
            if root not in path.parents or path.parent != root:
                raise RuntimeError("artifact revision is outside its contest directory")
            known[path.name] = path
        entries = list(root.iterdir())
        if any(entry.name not in known for entry in entries):
            raise RuntimeError("artifact workspace contains an untracked entry")
        active = str(contest.get("active_material_revision") or "")
        for name, path in sorted(known.items()):
            if name != active:
                self._remove_tree(path)

    def _purge_evidence(self, contest: dict) -> None:
        """Delete local evidence only after the longer retention boundary."""
        tid = str(contest["tid"])
        for root in (self.artifact_root, self.materials_dir, self.collected_dir):
            self._remove_tree(self._contest_path(root, tid))

    def sweep(self, *, now_ms: int | None = None) -> dict:
        observed = int(time.time() * 1000) if now_ms is None else int(now_ms)
        candidates = self.store.retention_candidates(
            now_ms=observed,
            workspace_retention_days=self.workspace_days,
            evidence_retention_days=self.evidence_days,
        )
        result = {"checked": len(candidates), "workspace": 0, "evidence": 0}
        workspace_boundary = self.workspace_days * 86_400_000
        evidence_boundary = self.evidence_days * 86_400_000
        for candidate in candidates:
            tid = str(candidate["tid"])
            current = self.store.get_contest(tid)
            if (
                not current
                or str(current.get("state") or "") not in {"safe_ended", "done"}
                or int(current.get("shutdown_verified_at_ms") or 0) <= 0
            ):
                continue
            ended = int(current["shutdown_verified_at_ms"])
            if (
                not int(current.get("workspace_purged_at_ms") or 0)
                and observed - ended >= workspace_boundary
            ):
                self._purge_workspace(current)
                if not self.store.mark_retention_purged(
                    tid, kind="workspace", observed_at_ms=observed
                ):
                    raise RuntimeError("workspace retention marker was not committed")
                result["workspace"] += 1
                current = self.store.get_contest(tid) or current
            if (
                not int(current.get("evidence_purged_at_ms") or 0)
                and observed - ended >= evidence_boundary
            ):
                self._purge_evidence(current)
                if not self.store.mark_retention_purged(
                    tid, kind="evidence", observed_at_ms=observed
                ):
                    raise RuntimeError("evidence retention marker was not committed")
                result["evidence"] += 1
        return result
