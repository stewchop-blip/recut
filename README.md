# Recut (MVP v1)

Telegram bot: текст → голосовое озвучка (TTS через OpenRouter).

## Быстрый старт

```bash
# 1. Копируем пример .env и заполняем (НЕ коммить секреты!)
cp .env.example .env

# 2. Запуск локально (PostgreSQL + бот)
docker-compose up --build

# 3. Или просто тест импорта
python -c "from app.core.config import get_settings; print('OK')"
```

## Структура

- `app/core/` — config, logging, limits
- `app/database/` — SQLAlchemy async (User, Generation)
- `app/services/tts/` — TTS абстракция + OpenRouter
- `app/services/text/` — LLM переписывание текста
- `app/services/media/` — FFmpeg (будущее видео)
- `app/bot/` — aiogram 3 handlers (thin)
- `tests/` — pytest (добавить)

## Переменные (.env)

`TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, `DATABASE_URL`, `WEBHOOK_SECRET`, `RAILWAY_PUBLIC_DOMAIN`

## Безопасность

- Нет секретов в коде (`grep -rni 'sk-' app/` → пусто)
- `.env` в `.gitignore`
- Temp файлы в `/tmp/recut/{job_id}/` с очисткой
- Webhook `secret_token` валидация

## Следующее

- Phase 4: текст переписывание (rewriter уже готов, подключить в bot)
- Phase 5: тесты
- Phase 6: Railway деплой + healthz