# Развёртывание на сервере

Проект запускается через Docker Compose: API, Redis и фоновый обработчик очереди используют один образ. SQLite-файлы хранятся в data/processed на сервере; исходные CSV монтируются только для чтения из data/raw.

## Требования

- Linux-сервер с Docker Engine и Docker Compose plugin.
- Исходные CSV набора данных. Эти файлы не включены в репозиторий.
- Ключ LLM-провайдера, если нужен анализ через модель. Без ключа проект использует режим правил.
- Reverse proxy с HTTPS (например, Nginx или Caddy) для публичного доступа.

## Первый запуск

Склонируйте репозиторий на сервер и создайте каталоги:

```sh
mkdir -p data/raw data/processed
cp .env.example .env
```

Скопируйте в data/raw файлы набора данных:

- df_Uid_data_hack.csv
- df_Uid_payment_hack.csv
- df_Fid_Uid_link_hack.csv

Проверьте владельца каталога, куда контейнер будет записывать SQLite:

```sh
sudo chown -R 10001:10001 data/processed
```

Отредактируйте .env и задайте LLM_API_KEY для модельного анализа. Compose переопределит REDIS_URL для связи контейнеров.

Соберите базу компаний и запустите сервисы:

```sh
docker compose --profile init run --rm init-data
docker compose up -d --build
docker compose ps
docker compose logs -f api
```

init-data нужно запускать при первичной подготовке базы и после обновления исходных CSV. Команда build пересобирает базу компаний. Чтобы обновить каталог новостей вручную:

```sh
docker compose run --rm --no-deps api python -m data_pipeline news-fetch --max-items 30
```

Чтобы поставить необработанные новости в очередь Redis:

```sh
docker compose run --rm --no-deps api python -m data_pipeline news-enqueue
```

Фоновый worker обрабатывает эту очередь. Анализы, запущенные через веб-интерфейс, выполняются API согласно текущей реализации приложения.

## Доступ и HTTPS

По умолчанию порт API привязан к 127.0.0.1:8000, чтобы публичный трафик проходил через reverse proxy. Настройте прокси на http://127.0.0.1:8000, включите HTTPS и передавайте заголовки Host, X-Forwarded-For и X-Forwarded-Proto.

Для временного доступа к API из сети задайте API_BIND_ADDRESS=0.0.0.0 и API_PORT=8000 в .env. Для постоянного публичного размещения используйте HTTPS reverse proxy и ограничьте доступ к служебным маршрутам.

## Обновление

```sh
git pull
docker compose build
docker compose up -d
docker compose ps
```

Compose сохраняет базы в data/processed и Redis в именованном томе sxsvn_redis_data. Перед обновлением или миграцией скопируйте каталог data/processed и сохраните Redis-данные, если очередь содержит важные задания.

## Диагностика

```sh
docker compose ps
docker compose logs --tail=200 api
docker compose logs --tail=200 worker
curl http://127.0.0.1:8000/api/v1/health
```

Если API сообщает, что базы компаний или новостей отсутствуют, проверьте CSV в data/raw/ и выполните docker compose --profile init run --rm init-data. Если контейнер не может создать SQLite, проверьте владельца data/processed (UID/GID 10001).
