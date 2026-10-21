# SXSVN insight engine

Модуль новостной бизнес-аналитики: извлечение фактов, оценка связи события с компанией, сбор доказательств и структурированный результат.

## Архитектура

- `models.py` — Pydantic-контракты входа, инструментов, evidence и результата;
- `agent.py` — bounded orchestration и состояние одного анализа;
- `facts.py` — heuristic/LLM extraction с кэшем;
- `relevance.py` — `KEEP` / `DROP` / `REVIEW`;
- `evidence.py`, `results.py` — evidence catalog и сборка результата;
- `validation.py`, `semantic.py` — структурные и смысловые проверки;
- `agent_tools.py` — только протокол внешних инструментов и fail-closed fallback;
- `llm.py` — OpenAI-compatible API adapter.

Production-код не содержит профилей компаний, платежей, новостей или банковских документов-заглушек. Внешний toolkit передаётся явно.

## Установка и тесты

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```

## Конфигурация LLM

```powershell
Copy-Item .env.example .env
# замените SXSVN_LLM_API_KEY на ключ команды
```

По умолчанию используются endpoint `https://changellenge.up.railway.app/v1` и модель `deepseek/deepseek-v4.1-flash`. Ключ не добавляется в Git и не выводится в логи.

## Вызов

```python
from ai_engine import insight_engine
from ai_engine.llm import OpenAICompatibleClient

result = insight_engine(context, llm=OpenAICompatibleClient(), toolkit=external_toolkit)
```

Без `toolkit` движок не создаёт фиктивные данные: внешние операции получают статус `error`, а результат помечается `needs_review`.

Контракты описаны в [docs/CONTRACTS.md](./docs/CONTRACTS.md).
