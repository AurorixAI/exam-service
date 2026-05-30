"""
Exam Service — Task & Textbook repositories.

Previously these queried content-db (PostgreSQL) directly via SQLAlchemy.
Now they call the content-service HTTP read API so that exam-service owns
only its own database (exam results, sessions).

Public method signatures are unchanged — router.py does not need to know
about the transport change.
"""
from __future__ import annotations

from typing import List, Optional

from src.clients import content_client

BASE = "/api/v1/content"


class TaskRepository:
    """Read tasks_master via content-service HTTP API."""

    def get_tasks_by_skills(
        self,
        skill_ids: List[str],
        difficulty: Optional[str] = None,
        limit: int = 50,
        exclude_ids: Optional[List[str]] = None,
    ) -> List[dict]:
        params: dict = {"limit": limit, "random_order": True}
        params["skill_ids"] = skill_ids
        if difficulty:
            params["difficulty"] = difficulty
        if exclude_ids:
            params["exclude_ids"] = exclude_ids
        resp = content_client.get(f"{BASE}/tasks", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_tasks_by_toc_ids(
        self,
        toc_ids: List[int],
        difficulty: Optional[str] = None,
        limit: int = 50,
        exclude_ids: Optional[List[str]] = None,
    ) -> List[dict]:
        params: dict = {"limit": limit, "random_order": True}
        params["toc_ids"] = toc_ids
        if difficulty:
            params["difficulty"] = difficulty
        if exclude_ids:
            params["exclude_ids"] = exclude_ids
        resp = content_client.get(f"{BASE}/tasks", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_task_by_id(self, task_id: str) -> Optional[dict]:
        resp = content_client.get(f"{BASE}/tasks/{task_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def expand_toc_ids(self, toc_ids: List[int]) -> List[int]:
        """Recursively expand toc_ids to include all child paragraphs."""
        resp = content_client.post(f"{BASE}/toc/expand", json={"toc_ids": toc_ids})
        resp.raise_for_status()
        return resp.json()


class TextbookRepository:
    """Read textbooks/toc/skill-map via content-service HTTP API."""

    def list_textbooks(self, class_level: Optional[int] = None) -> List[dict]:
        params: dict = {}
        if class_level:
            params["class_level"] = class_level
        resp = content_client.get(f"{BASE}/textbooks", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_toc(self, textbook_id: str) -> List[dict]:
        resp = content_client.get(f"{BASE}/textbooks/{textbook_id}/toc")
        resp.raise_for_status()
        return resp.json()

    def get_toc_skills(self, toc_id: int) -> List[dict]:
        resp = content_client.get(f"{BASE}/toc/{toc_id}/skills")
        resp.raise_for_status()
        return resp.json()

    def get_coverage(self, textbook_id: str) -> List[dict]:
        resp = content_client.get(f"{BASE}/textbooks/{textbook_id}/coverage")
        resp.raise_for_status()
        return resp.json()

    def get_by_id(self, textbook_id: str) -> Optional[dict]:
        resp = content_client.get(f"{BASE}/textbooks/{textbook_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_skill_tree(self, class_level: Optional[int] = None) -> List[dict]:
        params: dict = {"with_task_counts": True}
        if class_level:
            params["class_level"] = class_level
        resp = content_client.get(f"{BASE}/skills", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_tasks(
        self,
        textbook_id: str,
        *,
        toc_id: Optional[int] = None,
        skill_id: Optional[str] = None,
        difficulty: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[dict]:
        params: dict = {
            "textbook_id": textbook_id,
            "limit": limit,
            "offset": offset,
        }
        if toc_id is not None:
            params["toc_ids"] = [toc_id]
        if skill_id:
            params["skill_id"] = skill_id
        if difficulty:
            params["difficulty"] = difficulty
        resp = content_client.get(f"{BASE}/tasks", params=params)
        resp.raise_for_status()
        return resp.json()
