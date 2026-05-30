# exam-service

Сервис управления экзаменами. Учитель создаёт экзамен, генерирует варианты через Gemini AI, студенты проходят — сервис сохраняет ответы и считает результаты.

- **Port**: 8003
- **DB**: `algo_exam` (PostgreSQL 15)
- **Cache**: Redis (кэш шаблонов, TTL 1ч)
- **Framework**: FastAPI + SQLAlchemy ORM + Alembic
- **Main file**: `src/main.py`

## Переменные окружения

| Переменная | Обязательная | Описание | Пример |
|---|---|---|---|
| `DATABASE_URL` | ✅ | PostgreSQL DSN | `postgresql://algo:pass@algo-postgres-exam:5432/algo_exam` |
| `REDIS_URL` | ✅ | Redis DSN | `redis://algo-redis-exam:6379` |
| `GEMINI_API_KEY` | ✅ | Gemini AI (генерация вариантов) | `AIza...` |
| `APP_ENV` | — | `production` / `development` | `production` |
| `CONTENT_SERVICE_URL` | — | URL content-service | `http://content-api:8004` |
| `AUTH_SERVICE_URL` | — | URL auth-service | `http://algo-auth:8000` |
| `CORS_ORIGINS` | — | JSON-массив origins | `["https://algolab.me"]` |
| `MAX_TASKS_PER_EXAM` | — | Лимит заданий на экзамен | `50` |
| `MAX_VARIANTS` | — | Лимит вариантов | `30` |
| `DEFAULT_TIME_MINUTES` | — | Время по умолчанию (мин) | `45` |

## Как запустить

```bash
cp .env.example .env   # заполнить DATABASE_URL, REDIS_URL, GEMINI_API_KEY
docker compose up -d --build

# Миграции
docker exec algo-exam alembic upgrade head

# Prod
docker compose -f docker-compose.prod.yml up -d
```

## API

| Метод | Путь | Описание |
|---|---|---|
| GET/POST | `/api/v1/exams/` | Список / создать экзамен |
| GET/PUT/DELETE | `/api/v1/exams/{id}` | Управление экзаменом |
| POST | `/api/v1/exams/{id}/publish` | Опубликовать |
| GET | `/api/v1/exams/{id}/variants` | Варианты экзамена |
| POST | `/api/v1/exam/start` | Студент начинает экзамен |
| POST | `/api/v1/exam/{id}/answer` | Студент отвечает |
| GET | `/api/v1/exam/{id}/result` | Результат студента |
| GET | `/health/live` | Liveness probe |
| GET | `/health/ready` | Readiness probe (DB + Redis) |
| GET | `/metrics` | Prometheus метрики |

Swagger UI (только `development`): http://localhost:8003/docs

## Тесты

```bash
pip install -r requirements.txt pytest pytest-asyncio

# Unit тесты (без DB)
pytest tests/test_unit.py -v

# Все тесты (нужна запущенная DB)
pytest tests/ -v
```
