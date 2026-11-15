"""Превращение фактов в человеческий разбор: Claude API, Claude Code CLI или запасной текст без LLM."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from .config import Config

SYSTEM_PROMPT = """Ты — шахматный тренер, который пишет ученику короткий вечерний разбор в Telegram.

Правила языка:
- Никакого жаргона движка. Запрещены слова и обозначения: «оценка», «эвал», «сантипешки», «+1.5», «глубина», «линия движка», «PV», «Stockfish», «движок считает», «лучший ход по движку». Вместо цифр — «вы отдали пешку», «потеряли коня», «из выигранной позиции стало равно».
- Ходы называй по-человечески: «конь на f3», «взятие на e5 слоном». Нотацию (Nf3) можно дать в скобках, один раз.
- Пиши историями: что было на доске к этому моменту, что ученик сделал, как соперник этим воспользовался и что можно было сделать вместо этого. Одна история — один ключевой момент.

Правила честности:
- Используй ТОЛЬКО факты из данных. Не выдумывай угрозы, тактику и планы, которых нет в данных. Если в данных есть только «лучше было X» и «соперник ответил Y», так и расскажи, без домыслов о причинах.
- Мотивы в данных: hung_<фигура>_<поле> — фигура осталась под боем; double_attack — двойной удар соперника по перечисленным целям; allowed_mate_in_N — разрешил мат; missed_mate_in_N — пропустил свой мат; missed_material_win — упустил выигрыш материала; missed_punish — не наказал зевок соперника; low_clock — мало времени на часах; time_spent — секунды на ход.
- Время на часах — важная часть истории: если зевок сделан за 2 секунды при запасе времени, это спешка, а не цейтнот.

Формат (простой текст, без markdown, без звёздочек и решёток):
1. Одна строка-итог дня: сколько партий, результат.
2. 2–4 истории о ключевых моментах (самые дорогие ошибки и упущенные шансы), каждая 3–5 предложений.
3. Если есть паттерн, который повторяется (в сегодняшних партиях или в профиле слабых мест) — одно предложение о нём.
4. Ровно ОДНА рекомендация на следующую партию — конкретное, проверяемое действие («перед каждым взятием проверь, чем соперник бьёт в ответ»), а не общий совет «считай варианты». Начни её с «На следующую партию:».
Если в данных есть вчерашняя рекомендация — одной фразой скажи, получилось ли её выполнить.
Всего не больше 1800 символов."""

WEEKLY_PROMPT = """Ты — шахматный тренер. Напиши ученику недельную сводку для Telegram по данным ниже.
Те же правила, что и в ежедневном разборе: никакого жаргона движка, только факты из данных, простой текст без markdown.
Структура: итог недели (партии, результат, рейтинг) → что стало лучше/хуже по сравнению с прошлой неделей (по фазам, по времени, по типам ошибок) → самый дорогой момент недели одной историей → главная тема на следующую неделю и ОДНА рекомендация, начни её с «На следующую неделю:». До 2000 символов."""

PROFILE_PROMPT = """Ты — шахматный тренер. Обнови профиль слабых мест ученика по статистике за последние недели и прошлой версии профиля.
Правила: без жаргона движка, только то, что подтверждают цифры. Формат — простой текст:
- 3 главные слабости по убыванию важности; для каждой: в чём проявляется, цифра-доказательство, одно упражнение.
- 1–2 сильные стороны.
- Что изменилось с прошлой версии профиля (если она есть).
До 1500 символов."""


def _resolve_backend(cfg: Config) -> str:
    if cfg.llm_backend != "auto":
        return cfg.llm_backend
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "api"
    if shutil.which("claude"):
        return "claude-cli"
    return "none"


def _call_api(cfg: Config, system: str, user: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=cfg.anthropic_model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": cfg.anthropic_effort},
        # Если модель отклонит запрос, сервер сам повторит его на запасной модели.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Модель отказалась отвечать")
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _call_cli(system: str, user: str) -> str:
    prompt = system + "\n\n=== ДАННЫЕ ===\n" + user
    proc = subprocess.run(
        ["claude", "-p", "--output-format", "text"],
        input=prompt, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p завершился с кодом {proc.returncode}: {proc.stderr[-500:]}")
    return proc.stdout.strip()


def generate(cfg: Config, system: str, data: dict, fallback: str) -> str:
    """Вызвать выбранный бэкенд. При любой ошибке вернуть запасной текст, чтобы рассылка не падала."""
    backend = _resolve_backend(cfg)
    user = json.dumps(data, ensure_ascii=False, indent=1)
    try:
        if backend == "api":
            return _call_api(cfg, system, user)
        if backend == "claude-cli":
            return _call_cli(system, user)
    except Exception as e:  # noqa: BLE001 — не даём упасть рассылке
        return fallback + f"\n\n(разбор не сгенерирован: {type(e).__name__}: {str(e)[:200]})"
    return fallback


# ---------- для веб-интерфейса ----------

MOVE_PROMPT = """Ты — шахматный тренер. Объясни ученику ОДИН ход из его партии: что происходило на доске,
почему сыгранный ход хорош или плох, что было лучше и какая идея за лучшим ходом.
Правила: без жаргона движка (никаких «оценка +1.5», «глубина», «Stockfish»), только факты из данных —
не выдумывай угрозы, которых нет в данных о вариантах. Позиция дана в FEN, варианты — в нотации.
Простой текст, 4–7 предложений."""

GAME_REPORT_PROMPT = """Ты — шахматный тренер. Напиши отчёт о ОДНОЙ партии ученика по данным ниже.
Правила: без жаргона движка, только факты из данных, простой текст без markdown.
Структура: 1) как прошла партия в двух предложениях (дебют, перелом, финал); 2) лучшие моменты ученика;
3) 2–3 ключевые ошибки историями: что было на доске, что сыграно, как соперник ответил, что было лучше;
4) что эта партия говорит о слабых местах (если в данных есть профиль — свяжи с ним);
5) ОДНА конкретная рекомендация, начни её с «Что тренировать:». До 2200 символов."""

RESOURCES_PROMPT = """Найди в интернете лучшие материалы для изучения шахматного дебюта: «{name}» (играю за {color}).
Мой уровень: рейтинг около {rating} на Chess.com. Нужно {count} материалов: в основном видео на YouTube
(объяснения идей и типичных планов, а не только ходов), плюс 1–2 статьи или бесплатных курса
(например, Chess.com lessons/articles, lichess studies). Предпочтительно на русском, если хороших нет — на английском.
ОБЯЗАТЕЛЬНО используй веб-поиск и давай ТОЛЬКО ссылки, которые реально нашёл в результатах поиска. Не придумывай URL.
Ответь ТОЛЬКО JSON-массивом без пояснений вокруг:
[{{"title": "...", "url": "https://...", "kind": "video|article|course", "lang": "ru|en", "why": "чем полезно, одно предложение"}}]"""


def _call_cli_tools(prompt: str, tools: str = "WebSearch,WebFetch") -> str:
    proc = subprocess.run(
        ["claude", "-p", "--output-format", "text", "--allowedTools", tools],
        input=prompt, capture_output=True, text=True, timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p завершился с кодом {proc.returncode}: {proc.stderr[-500:]}")
    return proc.stdout.strip()


def _call_api_search(cfg: Config, prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": prompt}]
    for _ in range(5):  # web search может вернуть pause_turn — тогда продолжаем
        resp = client.messages.create(
            model=cfg.anthropic_model, max_tokens=16000, messages=messages,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
            output_config={"effort": cfg.anthropic_effort},
        )
        if resp.stop_reason != "pause_turn":
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        messages.append({"role": "assistant", "content": resp.content})
    raise RuntimeError("Поиск не завершился")


def ask(cfg: Config, system: str, data: dict) -> str:
    """Как generate, но ошибку пробрасываем — интерфейс сам покажет её пользователю."""
    backend = _resolve_backend(cfg)
    user = json.dumps(data, ensure_ascii=False, indent=1)
    if backend == "api":
        return _call_api(cfg, system, user)
    if backend == "claude-cli":
        return _call_cli(system, user)
    raise RuntimeError("LLM не настроен: нужен ANTHROPIC_API_KEY или установленный `claude`")


def find_resources(cfg: Config, name: str, color: str, rating: int | None, count: int = 6) -> list[dict]:
    prompt = RESOURCES_PROMPT.format(name=name, color="белых" if color == "white" else "чёрных",
                                     rating=rating or "1300", count=count)
    backend = _resolve_backend(cfg)
    if backend == "api":
        text = _call_api_search(cfg, prompt)
    elif backend == "claude-cli":
        text = _call_cli_tools(prompt)
    else:
        raise RuntimeError("LLM не настроен: нужен ANTHROPIC_API_KEY или установленный `claude`")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise RuntimeError("Не удалось разобрать ответ: " + text[:300])
    items = json.loads(text[start:end + 1])
    return [i for i in items if isinstance(i, dict) and str(i.get("url", "")).startswith("http")]
