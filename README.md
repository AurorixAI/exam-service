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

### Публичный вход ученика: защита от перебора (ROADMAP #33)

`POST /exams/by-code/{code}/check-student` и `/start` возвращают имя ученика при совпадении ID/ФИО, поэтому по коду экзамена
нельзя перебирать ID и собирать состав группы. Что сделано:

- Лимиты по паре (код экзамена, IP клиента) и по коду; IP — только `X-Real-IP` (его ставит nginx; `X-Forwarded-For` не доверяем),
  IPv6 считается по /64. Счётчики — Redis (общие для воркеров), без Redis — память процесса с одним предупреждением в лог.
- Строго считаются **неудачные** поиски (не найден / неоднозначно / имя не от этого ID): 15 в минуту и 60 в час на (код, IP).
  Все попытки (check + start) — только как защита от флуда: 120 в минуту и 400 в час на (код, IP). Класс из 30 человек за одним NAT
  (30 x check + start) проходит целиком. Если у кода набралось 100 неудач за час, отказ получают только IP, у которых уже >= 10 неудач.
- Ответ при превышении: `429`, заголовок `Retry-After`, текст «Слишком много попыток. Подождите минуту и попробуйте снова.»
  (при ожидании больше полутора минут — «Подождите N мин ...»).
- Любой отказ (нет такого ID, чужое имя к ID, однофамильцы, пустая группа) — один и тот же текст «Ученик не найден в этой группе — проверьте имя или ID»;
  причины auth-service до ученика не доходят. Имя возвращается только при точном однозначном совпадении (форма ответа не менялась).

### Привязка экзамена к занятию (ROADMAP #88)

`publish`, `assign` (с `publish`), `open-lobby` и подготовка печатного комплекта отказывают с `409` «Занятие уже прошло или отменено — выберите другое»,
если занятие завершено или отменено, или его день прошёл, или сегодня плановый конец уже наступил, а занятие не идёт
(`in_progress` проходит даже после планового конца; будущие дни проходят). Время — Asia/Tashkent; правило повторяет `isLessonBindable` во фронтенде.
Статус занятия берётся у auth-service (`/auth/org/groups/{id}/attendance`); если он недоступен — `503`, привязка не выполняется.

## Тесты

```bash
pip install -r requirements.txt pytest pytest-asyncio

# Unit тесты (без DB)
pytest tests/test_unit.py -v

# Все тесты (нужна запущенная DB)
pytest tests/ -v
```
