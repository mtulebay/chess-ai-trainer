# Шахматный AI-тренер

Chess.com → Stockfish (через python-chess) → разбор от Claude → Telegram, по расписанию из cron.

```
fetch    партии с api.chess.com (без ключа, по нику)           -> SQLite
analyze  каждый ход на глубине 16: потеря, лучший ход, фаза,      -> moves
         часы, проверяемые мотивы (подвисшая фигура, двойной удар, мат)
daily    партии с прошлого разбора (без пули) -> Claude -> Telegram
weekly   неделя против прошлой + переписанный профиль слабых мест -> Telegram
```

## Установка

```bash
git clone <repo> && cd chess-ai-trainer
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # заполнить ник, токен бота, chat_id
```

Stockfish: `apt install stockfish`, а без прав root:

```bash
.venv/bin/python -m chess_trainer setup-engine   # скачает релиз с GitHub в ./bin
```

Проверка и первый прогон:

```bash
.venv/bin/python -m chess_trainer check
.venv/bin/python -m chess_trainer fetch --months 3
.venv/bin/python -m chess_trainer analyze          # первый раз долго: ~10–30 с на партию
.venv/bin/python -m chess_trainer profile --narrative
.venv/bin/python -m chess_trainer daily --dry-run   # напечатать вместо отправки
```

Telegram: создать бота у @BotFather → токен; написать боту любое сообщение и взять `chat.id` из
`https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Кто пишет разбор (`LLM_BACKEND`)

- `claude-cli` — `claude -p` на сервере, работает по вашей подписке Claude Code, ключ API не нужен.
  В cron обязательно добавьте в `PATH` каталог, где лежит `claude`.
- `api` — Anthropic SDK, нужен `ANTHROPIC_API_KEY`. Модель `claude-opus-5-5`, включён серверный fallback на случай отказа модели.
- `none` — без LLM, сухой список ошибок.
- `auto` (по умолчанию): `api`, если есть ключ, иначе `claude-cli`, иначе `none`.

Если LLM упал, в Telegram всё равно уходит сухой отчёт с пометкой об ошибке, рассылка не теряется.

## Расписание

См. `crontab.example`: ежедневно в 22:30 `daily`, по пятницам в 21:00 `weekly`.
`scripts/run.sh` берёт lock (два запуска не пересекутся) и пишет лог в `logs/cron.log`.
Цифры профиля пересчитываются каждый вечер, текст профиля переписывается раз в неделю.

## Как считается

| | |
|---|---|
| Глубина | 16 полуходов (`ENGINE_DEPTH`) |
| Зевок / ошибка / неточность | потеря ≥ 2 / 1 / 0.5 пешки (`BLUNDER_CP` и т. д.) |
| Решённая позиция | если и до, и после хода перевес больше 5 пешек в одну сторону, потеря не считается ошибкой (`DECIDED_CP`) |
| Оценки | обрезаются до ±10 пешек, мат = +10 |
| Фаза | по материалу без пешек: эндшпиль ≤ 26 (или нет ферзей и ≤ 32), дебют: ход ≤ 12 и почти весь материал на доске |
| Цейтнот | на часах меньше max(30 с, 10% базового контроля) |
| Исключено | `bullet` (`EXCLUDE_TIME_CLASSES`), варианты вроде 960 |

Чтобы Claude не выдумывал тактику, в промпт передаются только проверенные факты: лучший ход и продолжение,
ответ соперника, сколько материала теряется, а также мотивы, которые вычислены кодом:
`hung_<фигура>_<поле>`, `double_attack`, `allowed_mate_in_N`, `missed_mate_in_N`, `missed_material_win`, `missed_punish`.

## Тесты

```bash
.venv/bin/python -m pytest -q
```
