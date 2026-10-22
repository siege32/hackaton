# Контракты модулей

Актуальные интеграционные контракты и маршрут сквозного запроса описаны в
[INTEGRATION.md](INTEGRATION.md). HTTP-маршруты и JSON-формы приведены в
[api/README.md](../api/README.md) и генерируемой FastAPI-схеме `/docs`.

Типы данных, статусы и сериализация определены кодом:

- `ai_engine/models.py` — `NewsRecord`, `CompanyProfile`, факты, evidence,
  статусы обработки и `InsightResult`;
- `ai_engine/storage/base.py` — протоколы репозиториев;
- `api/schemas.py` — входные HTTP-контракты;
- `data_pipeline/news/repository.py` и `data_pipeline/profiles.py` — адаптеры
  новостей и профилей из текущих SQLite-схем.

При расхождении документации и реализации источником истины являются
перечисленные типы, адаптеры и API-маршруты.
