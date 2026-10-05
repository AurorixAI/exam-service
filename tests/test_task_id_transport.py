"""Draft replacement must load the exact bank ID through HTTP parsing."""
import httpx
import pytest

from src.clients import content_client
from src.repositories.task_repo import TaskRepository


@pytest.mark.parametrize("task_id", [
    "ordinary-task", "G11_TB_?_265", "G11_TB_?_267_а", "G11_TB_?_268_а",
    "task#fragment&query=value+plus", "literal%3F%25", "задача с пробелом",
])
def test_task_repository_preserves_exact_id(monkeypatch, task_id):
    requests = []

    def serve(request):
        requests.append(request)
        if (request.url.path != "/api/v1/content/tasks/" + task_id
                or request.url.query or request.url.fragment):
            return httpx.Response(404)
        return httpx.Response(200, json={"id": task_id, "question_latex": "$x = 2$"})

    with httpx.Client(base_url="http://content.test", transport=httpx.MockTransport(serve)) as client:
        monkeypatch.setattr(content_client, "_client", client)
        task = TaskRepository().get_task_by_id(task_id)
    assert task is not None
    assert task["id"] == task["task_id"] == task_id
    assert task["question_latex"] == "$x = 2$"
    assert len(requests) == 1


def test_missing_task_remains_missing(monkeypatch):
    with httpx.Client(base_url="http://content.test", transport=httpx.MockTransport(
        lambda _: httpx.Response(404),
    )) as client:
        monkeypatch.setattr(content_client, "_client", client)
        assert TaskRepository().get_task_by_id("missing?task") is None
