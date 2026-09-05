"""Сквозные тесты режима витрины: апдейт Telegram -> роутер -> хэндлер -> ответ.

showcase_profiles передаётся во всех фикстурах явно (см. test_showcase_config.py) —
переменную окружения SHOWCASE_PROFILES из шелла _ignore_dotenv не отвязывает.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.bot.keyboards import NICHE_CALLBACK_PREFIX
from app.config import BASE_DIR, Settings
from app.core.niches import build_registry
from app.core.profile import load_profile
from app.core.prompts import CONTACT_BEFORE_NICHE_REPLY, NEED_NICHE_REPLY, SHOWCASE_INTRO
from app.db.crud import Repository
from tests.conftest import FakeLLM, FakeNotifier, text_response, tool_response
from tests.test_conversation import VALID_ARGS
from tests.test_integration import CHAT_ID, MockedSession, _callback_update, _make_dispatcher, _update


def _registry():
    profiles = [load_profile(slug, base_dir=BASE_DIR) for slug in ("tours", "rent_car")]
    return build_registry(profiles)


def _showcase_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        bot_token="123:TEST",
        admin_chat_ids="777, 888",
        llm_api_key="test-key",
        db_path=tmp_path / "test.sqlite3",
        knowledge_file=tmp_path / "missing.md",
        company_name="TestCo",
        company_business="аренда авто",
        manager_response_time="5 минут",
        showcase_profiles="tours, rent_car",
        message_aggregation_delay=0.0,
        throttle_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


async def test_start_with_showcase_shows_intro_and_menu(tmp_path, repo: Repository) -> None:
    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())

    await dispatcher.feed_update(bot, _update("/start"))

    assert llm.calls == []
    sent = [m for m in session.sent if isinstance(m, SendMessage)]
    assert sent and sent[0].text == SHOWCASE_INTRO
    assert isinstance(sent[0].reply_markup, InlineKeyboardMarkup)
    labels = [btn.text for row in sent[0].reply_markup.inline_keyboard for btn in row]
    assert labels == ["Экскурсии и трансферы", "Аренда авто"]


async def test_start_without_showcase_behaves_as_before(settings: Settings, repo: Repository) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(bot, _update("/start"))

    sent = [m for m in session.sent if isinstance(m, SendMessage)]
    assert "TestCo" in sent[0].text
    assert isinstance(sent[0].reply_markup, ReplyKeyboardMarkup)


async def test_niche_button_closes_spinner_and_sends_niche_welcome(
    tmp_path, repo: Repository
) -> None:
    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())
    # Кнопка появляется в чате только после /start, который уже завёл пользователя.
    await repo.upsert_user(chat_id=CHAT_ID, tg_user_id=CHAT_ID, username="ivan", full_name="Иван")

    await dispatcher.feed_update(
        bot, _callback_update(data=f"{NICHE_CALLBACK_PREFIX}tours")
    )

    assert any(isinstance(m, AnswerCallbackQuery) for m in session.sent), "спиннер не погашен"
    assert any(isinstance(m, EditMessageReplyMarkup) for m in session.sent), "кнопки плашки не убраны"
    tours = _registry().get("tours")
    assert tours is not None
    texts = [m.text for m in session.sent if isinstance(m, SendMessage)]
    assert texts and texts[-1] == tours.profile.welcome.strip()
    assert await repo.get_chat_profile(CHAT_ID) == "tours"


async def test_niche_button_unknown_slug_shows_menu_again(tmp_path, repo: Repository) -> None:
    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())

    await dispatcher.feed_update(
        bot, _callback_update(data=f"{NICHE_CALLBACK_PREFIX}no-such-slug")
    )

    assert any(isinstance(m, AnswerCallbackQuery) for m in session.sent), "спиннер не погашен"
    texts = [m.text for m in session.sent if isinstance(m, SendMessage)]
    assert texts and texts[-1] == SHOWCASE_INTRO
    assert await repo.get_chat_profile(CHAT_ID) is None


async def test_text_before_niche_selected_shows_menu_and_skips_llm(
    tmp_path, repo: Repository
) -> None:
    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())

    await dispatcher.feed_update(bot, _update("Хочу машину на выходные"))

    assert llm.calls == [], "текст до выбора ниши не должен доходить до модели"
    texts = [m.text for m in session.sent if isinstance(m, SendMessage)]
    assert texts and texts[-1] == SHOWCASE_INTRO
    assert await repo.get_history(CHAT_ID, limit=10, max_chars=10_000) == []


def _contact_update(update_id: int = 1) -> "Update":
    from datetime import datetime, timezone

    from aiogram.types import Chat, Contact, Message, Update, User

    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=CHAT_ID, type="private"),
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Иван", username="ivan"),
            contact=Contact(phone_number="+79991234567", first_name="Иван"),
        ),
    )


async def test_contact_before_niche_selected_shows_menu_without_lead(
    tmp_path, repo: Repository
) -> None:
    """Контакт, присланный до выбора направления, не должен создавать лид-заглушку:

    сейчас же не с какой нишей его сверить, а заглушка позже «съедала» бы дедупом
    настоящую заявку, которую клиент оставит после выбора направления.
    """
    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())

    await dispatcher.feed_update(bot, _contact_update())

    assert llm.calls == [], "контакт до выбора ниши не требует LLM"
    assert await repo.all_leads() == [], "лид-заглушка не должна создаваться"

    sent = list(session.sent)
    removals = [m for m in sent if isinstance(m, SendMessage) and isinstance(m.reply_markup, ReplyKeyboardRemove)]
    assert removals, "клавиатура «Отправить мой номер» должна быть убрана"
    assert removals[0].text == CONTACT_BEFORE_NICHE_REPLY

    texts = [m.text for m in sent if isinstance(m, SendMessage)]
    assert texts[-1] == SHOWCASE_INTRO, "после пояснения клиенту должно прийти меню выбора направления"


async def test_contact_then_niche_then_dialog_saves_real_qualified_lead(
    tmp_path, repo: Repository
) -> None:
    """Полный сценарий: контакт до выбора ниши -> выбор направления -> обычный
    диалог, доводящий до save_qualified_lead. Настоящая заявка не должна быть
    съедена дедупом заглушки, которую раньше создавал ранний контакт.
    """
    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    niches = _registry()
    llm = FakeLLM([tool_response("save_qualified_lead", VALID_ARGS), text_response("Спасибо, зафиксировал!")])
    notifier = FakeNotifier()
    from app.bot.factory import create_dispatcher
    from app.bot.services.conversation import ConversationService

    conversation = ConversationService(
        settings=settings, repo=repo, llm=llm, notifier=notifier, niches=niches  # type: ignore[arg-type]
    )
    dispatcher = create_dispatcher(settings=settings, repo=repo, conversation=conversation, niches=niches)

    # 1. Контакт до выбора ниши — лида быть не должно.
    await dispatcher.feed_update(bot, _contact_update(update_id=1))
    assert await repo.all_leads() == []

    # 2. Клиент выбирает направление.
    await dispatcher.feed_update(
        bot, _callback_update(data=f"{NICHE_CALLBACK_PREFIX}tours", update_id=2)
    )
    assert await repo.get_chat_profile(CHAT_ID) == "tours"

    # 3. Обычный диалог доводит модель до вызова save_qualified_lead.
    await dispatcher.feed_update(
        bot, _update("Иван, +79991234567, RAV4 на 12-19 августа", update_id=3)
    )

    leads = await repo.all_leads()
    assert len(leads) == 1, "должна быть ровно одна заявка — настоящая, не заглушка и не дубль"
    lead = leads[0]
    assert lead.profile_slug == "tours"
    assert lead.dates_or_timing != "не уточнено — спросить у клиента", (
        "в БД должна лежать квалифицированная заявка от LLM, а не заглушка"
    )
    assert len(notifier.sent) == 1


async def test_reset_keeps_selected_niche(tmp_path, repo: Repository) -> None:
    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())

    await repo.upsert_user(chat_id=CHAT_ID, tg_user_id=CHAT_ID, username="ivan", full_name="Иван")
    await repo.set_chat_profile(CHAT_ID, "tours")
    await repo.add_message(CHAT_ID, "user", "старое сообщение")

    await dispatcher.feed_update(bot, _update("/reset", update_id=2))

    assert await repo.get_chat_profile(CHAT_ID) == "tours"
    assert await repo.get_history(CHAT_ID, limit=10, max_chars=10_000) == []


async def test_forget_then_first_message_shows_menu_again(tmp_path, repo: Repository) -> None:
    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, llm, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())

    await repo.upsert_user(chat_id=CHAT_ID, tg_user_id=CHAT_ID, username="ivan", full_name="Иван")
    await repo.set_chat_profile(CHAT_ID, "tours")

    await dispatcher.feed_update(bot, _update("/forget", update_id=2))
    await dispatcher.feed_update(bot, _update("Привет ещё раз", update_id=3))

    assert llm.calls == []
    texts = [m.text for m in session.sent if isinstance(m, SendMessage)]
    assert texts[-1] == SHOWCASE_INTRO


async def test_help_mentions_niche_command_only_in_showcase(
    tmp_path, repo: Repository, settings: Settings
) -> None:
    showcase_settings = _showcase_settings(tmp_path)

    session_showcase = MockedSession()
    bot_showcase = Bot(token="123:TEST", session=session_showcase)
    dispatcher_showcase, _, _ = _make_dispatcher(
        showcase_settings, repo, bot_showcase, [], niches=_registry()
    )
    await dispatcher_showcase.feed_update(bot_showcase, _update("/help"))
    assert "/niche" in session_showcase.texts[-1]

    settings.throttle_enabled = False
    session_single = MockedSession()
    bot_single = Bot(token="123:TEST", session=session_single)
    dispatcher_single, _, _ = _make_dispatcher(settings, repo, bot_single, [])
    await dispatcher_single.feed_update(bot_single, _update("/help", update_id=2))
    assert "/niche" not in session_single.texts[-1]


async def test_niche_command_outside_showcase_replies_instead_of_silence(
    settings: Settings, repo: Repository
) -> None:
    settings.throttle_enabled = False
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [])

    await dispatcher.feed_update(bot, _update("/niche"))

    assert session.texts, "бот не должен молчать вне режима витрины"
    assert "одно направление" in session.texts[-1]


async def test_export_includes_niche_column_and_survives_missing_niche(
    tmp_path, repo: Repository
) -> None:
    from datetime import datetime, timezone

    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())

    await repo.create_lead(
        chat_id=CHAT_ID,
        tg_user_id=CHAT_ID,
        username="ivan",
        client_name="Иван",
        phone_or_contact="+79991234567",
        contact_normalized="tel:9991234567",
        dates_or_timing="завтра",
        service_details="экскурсия",
        budget=None,
        summary="тестовый лид",
        raw_payload={},
        profile_slug=None,
    )

    from aiogram.types import Chat, Message, Update, User

    await dispatcher.feed_update(
        bot,
        Update(
            update_id=5,
            message=Message(
                message_id=5,
                date=datetime.now(timezone.utc),
                chat=Chat(id=777, type="private"),
                from_user=User(id=777, is_bot=False, first_name="Admin"),
                text="/export",
            ),
        ),
    )

    docs = [m for m in session.sent if m.__class__.__name__ == "SendDocument"]
    assert docs, "выгрузка не отправлена"
    content = docs[0].document.data.decode("utf-8-sig")
    header = content.splitlines()[0]
    assert header.split(",")[-1] == "niche"


async def test_niche_button_on_inaccessible_message_still_sends_welcome(
    tmp_path, repo: Repository
) -> None:
    """Плашка старше 48 часов (или удалённая) приходит в callback.message как
    InaccessibleMessage — у него нет edit_reply_markup. Хэндлер не должен
    падать AttributeError'ом и обязан всё равно прислать приветствие ниши.
    """
    from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Update, User

    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    dispatcher, _, _ = _make_dispatcher(settings, repo, bot, [], niches=_registry())
    await repo.upsert_user(chat_id=CHAT_ID, tg_user_id=CHAT_ID, username="ivan", full_name="Иван")

    update = Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="1",
            from_user=User(id=CHAT_ID, is_bot=False, first_name="Иван", username="ivan"),
            chat_instance="1",
            data=f"{NICHE_CALLBACK_PREFIX}tours",
            message=InaccessibleMessage(chat=Chat(id=CHAT_ID, type="private"), message_id=1),
        ),
    )

    await dispatcher.feed_update(bot, update)

    assert any(isinstance(m, AnswerCallbackQuery) for m in session.sent), "спиннер не погашен"
    assert not any(isinstance(m, EditMessageReplyMarkup) for m in session.sent), (
        "снимать разметку недоступного сообщения нечем — не должно быть даже попытки в виде запроса"
    )
    tours = _registry().get("tours")
    assert tours is not None
    texts = [m.text for m in session.sent if isinstance(m, SendMessage)]
    assert texts and texts[-1] == tours.profile.welcome.strip()
    assert await repo.get_chat_profile(CHAT_ID) == "tours"


async def test_niche_switch_cancels_aggregator_before_switching(
    tmp_path, repo: Repository
) -> None:
    """`aggregator.cancel` обязан выполниться до `switch_niche`: иначе таймер
    флаша, дождавшийся лока чата уже после переключения, уедет в модель со
    старым текстом, но промптом и базой знаний новой ниши.
    """
    from app.bot.factory import create_dispatcher
    from app.bot.services.aggregator import MessageAggregator
    from app.bot.services.conversation import ConversationService

    settings = _showcase_settings(tmp_path)
    session = MockedSession()
    bot = Bot(token="123:TEST", session=session)
    niches = _registry()
    llm = FakeLLM([])
    notifier = FakeNotifier()
    conversation = ConversationService(
        settings=settings, repo=repo, llm=llm, notifier=notifier, niches=niches  # type: ignore[arg-type]
    )
    aggregator = MessageAggregator(delay=0.0)

    order: list[str] = []
    original_cancel = aggregator.cancel
    original_switch = conversation.switch_niche

    def spy_cancel(chat_id: int) -> None:
        order.append("cancel")
        return original_cancel(chat_id)

    async def spy_switch(chat_id: int, slug: str):
        order.append("switch")
        return await original_switch(chat_id, slug)

    aggregator.cancel = spy_cancel  # type: ignore[method-assign]
    conversation.switch_niche = spy_switch  # type: ignore[method-assign]

    dispatcher = create_dispatcher(
        settings=settings, repo=repo, conversation=conversation, aggregator=aggregator, niches=niches
    )

    await dispatcher.feed_update(bot, _callback_update(data=f"{NICHE_CALLBACK_PREFIX}tours"))

    assert order == ["cancel", "switch"], "буфер агрегатора должен сбрасываться до переключения ниши"
