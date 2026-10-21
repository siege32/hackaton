# HTTP API

Запуск из корня репозитория:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn api.main:app --reload
```

Swagger UI доступен по адресу `http://127.0.0.1:8000/docs`.

## `GET /health`

Проверка доступности процесса:

```json
{"status":"ok"}
```

## `POST /analyze`

Принимает `AnalysisContext` из `ai_engine.models` и возвращает `AnalysisResult`.
Обязательные поля: `company_id`, `event_id`, `as_of_date`, `profile` и
`event_facts`. Структуры этих объектов совпадают с моделями движка; неизвестные
поля отклоняются ответом `422`.

```json
{
  "company_id": "company-123",
  "event_id": "event-456",
  "as_of_date": "2026-10-21",
  "profile": {
    "company_id": "company-123",
    "name": "Название компании",
    "industries": ["retail"],
    "geographies": ["RU"],
    "counterparties": []
  },
  "event_facts": [
    {
      "fact_id": "fact-1",
      "text": "Текст подтвержденного факта",
      "source_ids": ["source-1"]
    }
  ],
  "sources": [
    {"source_id": "source-1", "title": "Источник", "kind": "news"}
  ]
}
```

При заданном `SXSVN_LLM_API_KEY` запрос использует настроенный OpenAI-compatible
клиент. Без ключа работает детерминированный режим движка. Внешние инструменты
пока не подключены: движок сообщает об их недоступности в `missing_data` и
может вернуть `needs_review`, не создавая фиктивных данных.

## `POST /v1/analyze/news`

Упрощённый вход первого сквозного сценария: профиль компании и одна `NewsItem`.
API добавляет источник новости и передаёт контекст в тот же движок. Если настроен
`SXSVN_LLM_API_KEY`, движок сам выполнит извлечение фактов и разбор; отдельный
контракт анализа не дублируется.

```json
{
  "event_id": "event-456",
  "as_of_date": "2026-10-21",
  "profile": {"company_id": "company-123", "name": "Название компании"},
  "news": {
    "news_id": "news-1",
    "text": "Полный текст новости",
    "source_id": "source-1",
    "title": "Заголовок",
    "url": "https://example.com/news/1",
    "published_at": "2026-10-21"
  }
}
```

## `POST /v1/news/parse`

Контракт подключения сервиса Данила. На входе должен быть передан ровно один
параметр: `source_url` или `raw_text`; дополнительно можно передать заголовок,
дату публикации и язык. Ответ содержит нормализованный `NewsItem`, связанный
`Source` и признак `duplicate`. Например:

```json
{"raw_text":"Текст статьи","title":"Заголовок","published_at":"2026-10-21"}
```

Сам парсер и хранение пока не реализованы. Пока адаптер `NewsParser` не передан
в `app.state.news_parser`, endpoint возвращает `503`. Реализация адаптера должна
вернуть `ParsedNewsResponse`; она отвечает за очистку, стабильный `news_id` и
`source_id`, дедупликацию и сохранение источника. Для URL-обработки API только
передаёт URL адаптеру и не загружает страницы самостоятельно.