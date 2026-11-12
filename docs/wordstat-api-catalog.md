# Wordstat: каталог API и границы данных

Проверено 12 ноября 2026. Текущий официальный маршрут — Wordstat в Yandex Search API. Каталог содержит **все четыре метода текущего WordstatService**: список REST-методов сопоставлен с [официальной protobuf-схемой](https://raw.githubusercontent.com/yandex-cloud/cloudapi/master/yandex/cloud/searchapi/v2/wordstat_service.proto). [Машиночитаемый каталог](../config/yandex-wordstat-methods.json) сохраняет точные пути, тип операции, тарификацию, поля, примеры и хеш проверенной схемы. Составление каталога не использовало ключи и не вызывало API аккаунта.

## Текущее состояние данных

Яндекс предупреждает: **статистика после 7 октября 2026 недоступна**, при этом методы продолжают отвечать. Значит, успешный ответ подтверждает подключение, но не актуальность частотностей за ноябрь. Это состояние провайдера на дату проверки, а не постоянный предел дат в нашем клиенте. Перед новым исследованием нужно сверить, снято ли предупреждение. [Официальный статус Wordstat](https://aistudio.yandex.ru/ru/docs/search-api/concepts/wordstat).

## Методы

Базовый адрес: `https://searchapi.api.cloud.yandex.net`. Все четыре операции используют HTTPS POST с JSON и **читают статистику**, не изменяя рекламу. Поле `paid` в каталоге отдельно отмечает тарификацию: POST и платный запрос сами по себе не означают запись рекламных объектов.

| ID в клиенте | Путь | Возможность | Цена одного запроса |
|---|---|---|---|
| [getTop](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/Wordstat/getTop) | `/v2/wordstat/topRequests` | Частотность, популярные формулировки и ассоциации | 0,02 ₽ |
| [getDynamics](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/Wordstat/getDynamics) | `/v2/wordstat/dynamics` | Динамика по дням, неделям или месяцам | 0,02 ₽ |
| [getRegionsDistribution](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/Wordstat/getRegionsDistribution) | `/v2/wordstat/regions` | Региональное распределение, доля и индекс интереса | 0,05 ₽ |
| [getRegionsTree](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/Wordstat/getRegionsTree) | `/v2/wordstat/getRegionsTree` | Дерево регионов и их коды | Не тарифицируется |

Цены с НДС: в [официальной таблице](https://aistudio.yandex.ru/ru/docs/search-api/pricing) они указаны за 1000 запросов — 20 ₽, 20 ₽ и 50 ₽ соответственно. Единица начисления — один запрос. Один вызов каждого метода даёт расчётную стоимость **0,09 ₽**, без учёта скидок и фактических условий аккаунта. Это оценка, не подтверждение списания. Внутренние ошибки сервера и ошибки авторизации, согласно тарифу, не оплачиваются.

Есть эквивалентный gRPC-сервис `yandex.cloud.searchapi.v2.WordstatService`. Другие сервисы Search API — веб-поиск, генеративные ответы, поиск изображений — не являются дополнительными методами Wordstat.

## Доступ

Использовать `Authorization: Api-Key …` для API-ключа сервисного аккаунта либо `Authorization: Bearer …` для IAM-токена. Нужны роль `search-api.webSearch.user` и для API-ключа область `yc.search-api.execute`. OAuth-токен Директа не подставлять. [Инструкция Wordstat](https://aistudio.yandex.ru/ru/docs/search-api/operations/wordstat-gettop).

`folderId` поддерживается во всех телах. Для сервисного аккаунта каталог определяется автоматически, если поле отсутствует; для пользовательского IAM-токена задавать нужный каталог явно. Это подтверждено разделом Yandex Search API v2 в [инструкции аутентификации](https://aistudio.yandex.ru/ru/docs/ai-studio/api-ref/authentication). Ключи и заголовок Authorization в каталог и результаты не сохранять.

## Параметры и реальные ограничения

- **Top:** обязательна `phrase`; доступны `numPhrases`, `regions`, `devices`, `folderId`. `numPhrases` — от 1 до 2000, по умолчанию 50. Установка 1 в проверке ниже уменьшает ответ, но не является ограничением клиента. Возвращаются `totalCount`, `results` и `associations`; целые числа могут приходить JSON-строками. [Схема метода](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/Wordstat/getTop).
- **Dynamics:** обязательны `phrase`, `period`, `fromDate`; доступны `toDate`, `regions`, `devices`, `folderId`. Даты — RFC3339. Для недели нужны границы понедельник–воскресенье, для месяца — первое и последнее числа. По дням поддерживаются все поисковые операторы, по неделям/месяцам — только `+`. Эти правила нельзя заменять общим запретом операторов. [Руководство](https://aistudio.yandex.ru/ru/docs/search-api/operations/wordstat-getdynamics).
- **Distribution:** обязательна `phrase`; поле называется `region`, а не старое `regionType`. Значения `REGION_ALL`, `REGION_CITIES`, `REGION_REGIONS`; доступны `devices` и `folderId`. [Схема метода](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/Wordstat/getRegionsDistribution).
- **Tree:** только необязательное `folderId`, ответ — рекурсивный массив `regions`. [Схема метода](https://aistudio.yandex.ru/ru/docs/search-api/api-ref/Wordstat/getRegionsTree).

В схемах phrase ограничена 400 символами; для Top/Dynamics — до 100 регионов и до трёх значений devices. Значения устройств: `DEVICE_ALL`, `DEVICE_DESKTOP`, `DEVICE_PHONE`, `DEVICE_TABLET`. Параметры не нужно молча обрезать или заменять: передавать заданные поля и разбирать ответ провайдера.

Публичные **изменяемые квоты Cloud** Wordstat — 10 запросов в секунду и 100 в час; фактические квоты конкретного аккаунта здесь не читались и могут отличаться. Повышение выполняется через провайдера. Жёсткий предел ассоциаций — 20. Общий предел веб-поиска 250 результатов нельзя переносить на Wordstat Top, у которого свой `numPhrases` до 2000. [Квоты и лимиты](https://aistudio.yandex.ru/ru/docs/search-api/concepts/limits).

В текущей схеме нет параметра пагинации Wordstat. Top — ограниченный список популярных запросов, а не весь спрос. Нет отдельного текущего метода остатка квоты в WordstatService. Не переносить прежние лимиты «1000 запросов в сутки», обязательную внутреннюю часовую квоту, лимит размера исследования или старый реестр допусков. Реальные ответы 429/RESOURCE_EXHAUSTED требуют учёта, а не обхода; повторные платные вызовы нельзя запускать бесконтрольно.

## Минимальная проверка подключения

Ниже рекомендуемые тела для существующего сервисного аккаунта; они сами не являются результатом вызова. Все четыре примера также находятся в `smoke_body` машиночитаемого каталога. Регион 225 перед рабочим исследованием сверить с актуальным деревом.

```json
{}
```

Для `getTop`:

```json
{"phrase":"внедрение битрикс24","numPhrases":1,"regions":["225"],"devices":["DEVICE_ALL"]}
```

Для `getDynamics` выбран закрытый месяц до даты ограничения данных:

```json
{"phrase":"внедрение битрикс24","period":"PERIOD_MONTHLY","fromDate":"2026-09-01T00:00:00Z","toDate":"2026-09-30T00:00:00Z","regions":["225"],"devices":["DEVICE_ALL"]}
```

Для `getRegionsDistribution`:

```json
{"phrase":"внедрение битрикс24","region":"REGION_REGIONS","devices":["DEVICE_ALL"]}
```

Формат Top/Distribution описывает последние 30 дней, но при текущем предупреждении нельзя подписывать ответ как проверенный свежий спрос. Сохранять точные параметры, момент получения и ограничения периода. Нулевые или отсутствующие строки не подменяют проверку полноты.

## Старый прямой API

Русская [официальная справка](https://yandex.ru/support2/wordstat/ru/content/api-wordstat) направляет пользователей в Yandex Search API. В старой англоязычной странице ещё встречаются пути `api.wordstat.yandex.net/v1/...`; это не доказательство их нынешней работоспособности. Их не включаем в исполняемый каталог и не пытаемся незаметно использовать другой токен. Текущий каталог полон на уровне четырёх опубликованных методов WordstatService, а не всех функций сайта или исторических API.
