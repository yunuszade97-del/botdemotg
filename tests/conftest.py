from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.bot.services.conversation import ConversationService
from app.config import Settings
from app.core.llm_client import LLMResponse, ToolCall
from app.db.crud import Repository
from app.db.database import Database
from app.db.models import Lead


@pytest.fixture(scope="session", autouse=True)
def _ignore_dotenv() -> None:
    """Отвязывает тесты от .env разработчика.

    Settings читает .env как источник более низкого приоритета, поэтому любое
    поле, не переданное в конструктор явно, приезжает из личного файла. Набор
    начинает вести себя по-разному локально и в CI, где .env нет, — и зелёные
    тесты перестают что-либо доказывать.
    """
    Settings.model_config["env_file"] = None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="123:TEST",
        admin_chat_ids="777, 888",
        llm_api_key="test-key",
        db_path=tmp_path / "test.sqlite3",
        knowledge_file=tmp_path / "missing.md",
        company_name="TestCo",
        company_business="аренда авто",
        manager_response_time="5 минут",
        lead_dedup_window_minutes=180,
        # Склейка сообщений отключена: тесты должны быть детерминированными
        # и не ждать реальных пауз. Сама склейка проверяется отдельно.
        message_aggregation_delay=0.0,
    )


@pytest.fixture
async def database(settings: Settings):
    db = Database(settings.db_path)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def repo(database: Database) -> Repository:
    return Repository(database)


class FakeLLM:
    """Подменяет LLMClient: отдаёт заранее заданные ответы по очереди."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, messages, tools=None, tool_choice="auto") -> LLMResponse:
        # Копия: сервис продолжает мутировать список после возврата.
        self.calls.append([dict(m) for m in messages])
        if not self.responses:
            raise AssertionError("FakeLLM: запрошено больше ответов, чем задано")
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


@dataclass
class FakeNotifier:
    sent: list[Lead] = field(default_factory=list)
    alerts: list[tuple[str, str]] = field(default_factory=list)

    async def notify(self, lead: Lead) -> bool:
        self.sent.append(lead)
        return True

    async def alert(self, key: str, text: str) -> bool:
        self.alerts.append((key, text))
        return True

    async def flush_pending(self, limit: int = 50) -> int:
        return 0


def text_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, tool_calls=[], raw_message={"role": "assistant", "content": content}
    )


def tool_response(name: str, arguments: str, call_id: str = "call_1") -> LLMResponse:
    call = ToolCall(id=call_id, name=name, arguments=arguments)
    return LLMResponse(
        content=None,
        tool_calls=[call],
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            ],
        },
    )


@pytest.fixture
def make_service(settings: Settings, repo: Repository):
    def _factory(responses: list[LLMResponse]) -> tuple[ConversationService, FakeLLM, FakeNotifier]:
        llm = FakeLLM(responses)
        notifier = FakeNotifier()
        service = ConversationService(
            settings=settings, repo=repo, llm=llm, notifier=notifier  # type: ignore[arg-type]
        )
        return service, llm, notifier

    return _factory
