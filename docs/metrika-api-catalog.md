# Каталог API Яндекс Метрики

Проверено 12 ноября 2026 года по официальному оглавлению и страницам методов (редакция документации `r21770580`). Каталог содержит **110 документированных операций: 50 чтения и 60 изменяющих**. Это 108 страниц справочника плюс два способа отправки событий Measurement Protocol — GET и POST. Одна страница списка загрузок чатов противоречива и отдельно помечена; её маршрут не считать подтверждённым. Чаты выделены отдельными страницами, но используют общие маршруты офлайн-конверсий; `type=CHATS` указан для загрузки. Поэтому число операций не равно числу уникальных URL.

[Машиночитаемый каталог](../config/yandex-metrika-methods.json) содержит точные method/path, origin, вид авторизации, эффект, источник и SHA-256 прочитанной страницы. Он извлечён из опубликованного оглавления и Markdown OpenAPI-страниц; единая исходная OpenAPI JSON/YAML-схема не загружалась. Это полнота по текущему оглавлению методов, а не обещание всех функций интерфейса, всех исторических API или всех сочетаний полей.

Каталог составлен без токенов и без запросов к аккаунту. Результаты реального подключения фиксируются отдельно; наличие метода в каталоге не доказывает права конкретного аккаунта.

## Состав

| Раздел | Чтение | Изменяющие операции | Возможности |
|---|---:|---:|---|
| Управление | 25 | 37 | Счётчики, цели, фильтры, операции преобразования URL, сегменты, метки, примечания, аккаунты, представители, доступы, клиенты Директа, экспорт в Cloud |
| Импорт данных | 15 | 18 | Статусы загрузок и импорт расходов, CRM, звонков, офлайн-конверсий, чатов, параметров посетителей; управление токенами Measurement Protocol |
| Отчёты | 6 | 0 | Таблица, динамика, сводная таблица, drill-down, сравнение сегментов и сравнение drill-down |
| Logs | 4 | 3 | Список/статус/оценка/скачивание; создание, очистка и отмена выгрузки |
| Measurement Protocol collector | 0 | 2 | Отправка просмотров, событий, целей и ecommerce через GET или POST; пакетная отправка POST |

## Семантика и транспорт

- Основной origin — `https://api-metrika.yandex.net`; OAuth передаётся только в заголовке `Authorization: OAuth …`. [Права](https://yandex.ru/dev/metrika/ru/intro/authorization): чтение `metrika:read`, изменения `metrika:write`; отдельные права предусмотрены для расходов, параметров пользователей и офлайн-данных. Наличие права не является текущим поручением на запись.
- **GET не всегда означает чтение.** [measurement/generate](https://yandex.ru/dev/metrika/ru/management/openapi/measurement/generateToken) создаёт дополнительный секретный токен и отмечен `write`. Его нельзя включать в автоматическую проверку всех GET.
- [Measurement Protocol](https://yandex.ru/dev/metrika/ru/data-import/measurement-upload) находится на другом origin `https://mc.yandex.ru/collect`. Оба HTTP-метода изменяют аналитику. Нужен отдельный секрет `ms`; OAuth основного API туда не передаётся. POST допускает несколько URL-encoded строк событий в теле. Успех приёма не доказывает появление целевой метрики в отчёте.
- Создание [Logs-запроса](https://yandex.ru/dev/metrika/ru/logs/practice/quick-start) изменяет состояние серверной задачи и расходует место подготовленных выгрузок; в каталоге это `write` с `effect_detail=export_job_state`, хотя рекламные объекты оно не меняет. Оценка `evaluate` — чтение. Автоматически создавать/чистить/отменять задачи при проверке чтения не следует.
- [Logs download](https://yandex.ru/dev/metrika/ru/logs/openapi/download) описан как `string/binary`. Сохранять байты, не требовать JSON, несмотря на сгенерированную подпись `application/json`. Обычные методы возвращают JSON; способы импорта включают JSON, CSV, multipart/form-data — нужный формат приведён на странице операции.
- Для таблицы отчёта документирован [CSV](https://yandex.ru/dev/metrika/ru/stat/) через `/stat/v1/data.csv`; в JSON он указан как вариант представления `stat_data_1`, без увеличения числа операций.
- Обычное чтение счётчика с `field=measurement_tokens` возвращает действительные секреты. При проверке подключения это поле не запрашивать; ответы с секретами не печатать и не синхронизировать.
- Параметры фильтров, метрик, группировок, атрибуции, дат и пагинации не урезаются каталогом. Их сочетания определяет метод. Фиксированный `required_query.type=CHATS` отличает загрузку чат-конверсий от соседнего импорта; GET списка и детали описаны как общие для офлайн-конверсий, включая чаты.
- [Страница списка загрузок чатов](https://yandex.ru/dev/metrika/ru/management/openapi/chats/findAll_chats) публикует `uploadings/{uploadingId}`, но описывает список и только `counterId` среди path parameters. В JSON оставлено точное свидетельство с `documentation_status=inconsistent`; для чтения списка использовать [общий список загрузок офлайн-конверсий](https://yandex.ru/dev/metrika/ru/management/openapi/offline_conversions/findAll_1), который не требует ID. Подмена сомнительного маршрута «по догадке» не выполнялась.
- Отчёты могут быть неполными из-за лимита строк или [семплирования](https://yandex.ru/dev/metrika/ru/stat/sampling). Сохранять `sampled`, `sample_share`, `sample_size`, `sample_space`, `total_rows` и параметры запроса. `accuracy=full` можно запросить явно; отсутствие строк не заменяет проверку периода/фильтров/цели.

## Реальные пределы провайдера

Это сведения о Яндексе, не локальные ограничения команды. Их нельзя переносить в произвольную квоту проекта или молча обрезать задачу.

- [Квоты API](https://yandex.ru/dev/metrika/ru/intro/quotas): 30 запросов/сек с IP для API, 10 для Logs; 3 параллельных запроса пользователя; 5000 запросов пользователя в сутки. Для API отчётов указано 200 запросов за 5 минут. При превышении Яндекс возвращает HTTP 429 с типом квоты.
- [Logs](https://yandex.ru/dev/metrika/ru/logs/): период одного запроса до года, строка `fields` до 3000 символов, совокупное хранилище подготовленных выгрузок 10 ГБ; текущий день недоступен. Увеличение квоты относится к Метрике Про. Это не запрет анализировать больший период последовательными подходящими запросами.
- [Measurement Protocol](https://yandex.ru/dev/metrika/ru/data-import/measurement-upload): тело пакета до 1 МБ в несжатом виде, число строк отдельным пределом не ограничено. Документация предупреждает о потере данных сверх размера даже при HTTP 200. Приём прошлого события ограничен 12 часами. [Генерация токенов](https://yandex.ru/dev/metrika/ru/management/openapi/measurement/generateToken): максимум пять активных токенов счётчика.
- У методов управления и отчётов свои размеры страниц и полей. Например, [список счётчиков](https://yandex.ru/dev/metrika/ru/management/openapi/counter/counters) допускает `per_page` до 10000. При работе сверять страницу выбранного метода; локальная подготовка не заменяет серверную валидацию.

## Указатель методов

R — чтение, W — изменение данных, настроек, секретов или состояния задач. Полные маршруты и стабильные ID — в JSON.

### API управления

| Группа | R | W |
|---|---|---|
| Управление метками | [label_getLabel](https://yandex.ru/dev/metrika/ru/management/openapi/label/getLabel), [label_getLabels](https://yandex.ru/dev/metrika/ru/management/openapi/label/getLabels) | [label_updateLabel](https://yandex.ru/dev/metrika/ru/management/openapi/label/updateLabel), [label_deleteLabel](https://yandex.ru/dev/metrika/ru/management/openapi/label/deleteLabel), [label_createLabel](https://yandex.ru/dev/metrika/ru/management/openapi/label/createLabel) |
| Управление счетчиками | [counter_counter](https://yandex.ru/dev/metrika/ru/management/openapi/counter/counter), [counter_counters](https://yandex.ru/dev/metrika/ru/management/openapi/counter/counters) | [counter_editCounter](https://yandex.ru/dev/metrika/ru/management/openapi/counter/editCounter), [counter_deleteCounter](https://yandex.ru/dev/metrika/ru/management/openapi/counter/deleteCounter), [counter_addCounter](https://yandex.ru/dev/metrika/ru/management/openapi/counter/addCounter), [counter_undeleteCounter](https://yandex.ru/dev/metrika/ru/management/openapi/counter/undeleteCounter) |
| Управление операциями | [operation_operation](https://yandex.ru/dev/metrika/ru/management/openapi/operation/operation), [operation_operations](https://yandex.ru/dev/metrika/ru/management/openapi/operation/operations) | [operation_editOperation](https://yandex.ru/dev/metrika/ru/management/openapi/operation/editOperation), [operation_deleteOperation](https://yandex.ru/dev/metrika/ru/management/openapi/operation/deleteOperation), [operation_addOperation](https://yandex.ru/dev/metrika/ru/management/openapi/operation/addOperation) |
| Управление доступами | [grant_grants](https://yandex.ru/dev/metrika/ru/management/openapi/grant/grants), [grant_grant](https://yandex.ru/dev/metrika/ru/management/openapi/grant/grant), [grant_myGrant](https://yandex.ru/dev/metrika/ru/management/openapi/grant/myGrant) | — |
| Публичный доступ | — | [grant_addPublicGrant](https://yandex.ru/dev/metrika/ru/management/openapi/grant/addPublicGrant), [grant_deletePublicGrant](https://yandex.ru/dev/metrika/ru/management/openapi/grant/deletePublicGrant) |
| Индивидуальный доступ | — | [grant_addGrant](https://yandex.ru/dev/metrika/ru/management/openapi/grant/addGrant), [grant_editGrant](https://yandex.ru/dev/metrika/ru/management/openapi/grant/editGrant), [grant_deleteGrant](https://yandex.ru/dev/metrika/ru/management/openapi/grant/deleteGrant) |
| Управление фильтрами доступа | [access_filter_getAccessFilter](https://yandex.ru/dev/metrika/ru/management/openapi/access_filter/getAccessFilter), [access_filter_getAccessFiltersForCounter](https://yandex.ru/dev/metrika/ru/management/openapi/access_filter/getAccessFiltersForCounter) | [access_filter_createAccessFilter](https://yandex.ru/dev/metrika/ru/management/openapi/access_filter/createAccessFilter), [access_filter_updateAccessFilter](https://yandex.ru/dev/metrika/ru/management/openapi/access_filter/updateAccessFilter), [access_filter_deleteAccessFilter](https://yandex.ru/dev/metrika/ru/management/openapi/access_filter/deleteAccessFilter) |
| Управление целями | [goal_goal](https://yandex.ru/dev/metrika/ru/management/openapi/goal/goal), [goal_goals](https://yandex.ru/dev/metrika/ru/management/openapi/goal/goals), [goal_getSocialNetworks](https://yandex.ru/dev/metrika/ru/management/openapi/goal/getSocialNetworks), [goal_getMessengers](https://yandex.ru/dev/metrika/ru/management/openapi/goal/getMessengers) | [goal_editGoal](https://yandex.ru/dev/metrika/ru/management/openapi/goal/editGoal), [goal_deleteGoal](https://yandex.ru/dev/metrika/ru/management/openapi/goal/deleteGoal), [goal_addGoal](https://yandex.ru/dev/metrika/ru/management/openapi/goal/addGoal) |
| Управление фильтрами | [filter_filter](https://yandex.ru/dev/metrika/ru/management/openapi/filter/filter), [filter_filters](https://yandex.ru/dev/metrika/ru/management/openapi/filter/filters) | [filter_editFilter](https://yandex.ru/dev/metrika/ru/management/openapi/filter/editFilter), [filter_deleteFilter](https://yandex.ru/dev/metrika/ru/management/openapi/filter/deleteFilter), [filter_addFilter](https://yandex.ru/dev/metrika/ru/management/openapi/filter/addFilter) |
| Примечания на графиках | [chart_annotation_get](https://yandex.ru/dev/metrika/ru/management/openapi/chart_annotation/get), [chart_annotation_findAll_3](https://yandex.ru/dev/metrika/ru/management/openapi/chart_annotation/findAll_3) | [chart_annotation_update_1](https://yandex.ru/dev/metrika/ru/management/openapi/chart_annotation/update_1), [chart_annotation_delete](https://yandex.ru/dev/metrika/ru/management/openapi/chart_annotation/delete), [chart_annotation_create](https://yandex.ru/dev/metrika/ru/management/openapi/chart_annotation/create) |
| Управление сегментами | [segment_getSegment](https://yandex.ru/dev/metrika/ru/management/openapi/segment/getSegment), [segment_getSegmentsForCounter](https://yandex.ru/dev/metrika/ru/management/openapi/segment/getSegmentsForCounter) | [segment_updateSegment](https://yandex.ru/dev/metrika/ru/management/openapi/segment/updateSegment), [segment_deleteSegment](https://yandex.ru/dev/metrika/ru/management/openapi/segment/deleteSegment), [segment_createSegment](https://yandex.ru/dev/metrika/ru/management/openapi/segment/createSegment) |
| Управление аккаунтами | [account_accounts](https://yandex.ru/dev/metrika/ru/management/openapi/account/accounts) | [account_updateAccounts](https://yandex.ru/dev/metrika/ru/management/openapi/account/updateAccounts), [account_deleteAccount](https://yandex.ru/dev/metrika/ru/management/openapi/account/deleteAccount) |
| Управление представителями | [delegates_delegates](https://yandex.ru/dev/metrika/ru/management/openapi/delegates/delegates) | [delegates_addDelegate](https://yandex.ru/dev/metrika/ru/management/openapi/delegates/addDelegate), [delegates_deleteDelegate](https://yandex.ru/dev/metrika/ru/management/openapi/delegates/deleteDelegate) |
| Привязка счетчиков к меткам | — | [counter_label_setCounterLabel](https://yandex.ru/dev/metrika/ru/management/openapi/counter-label/setCounterLabel), [counter_label_unsetCounterLabel](https://yandex.ru/dev/metrika/ru/management/openapi/counter-label/unsetCounterLabel) |
| Управление клиентами Директа | [clients_getClients](https://yandex.ru/dev/metrika/ru/management/openapi/clients/getClients) | — |
| Управление экспортом в Yandex Cloud | [cloud_getExportsByCounter](https://yandex.ru/dev/metrika/ru/management/openapi/cloud/getExportsByCounter) | [cloud_removeCounter](https://yandex.ru/dev/metrika/ru/management/openapi/cloud/removeCounter) |

### API импорта данных

| Группа | R | W |
|---|---|---|
| Импорт расходов | [expense_findAll_2](https://yandex.ru/dev/metrika/ru/management/openapi/expense/findAll_2), [expense_findById_2](https://yandex.ru/dev/metrika/ru/management/openapi/expense/findById_2) | [expense_uploadMultipart](https://yandex.ru/dev/metrika/ru/management/openapi/expense/uploadMultipart), [expense_uploadRemoveSingleLine](https://yandex.ru/dev/metrika/ru/management/openapi/expense/uploadRemoveSingleLine), [expense_uploadRemoveBody_1](https://yandex.ru/dev/metrika/ru/management/openapi/expense/uploadRemoveBody_1) |
| Упрощенная отправка данных | — | [schema_uploadSimpleOrders](https://yandex.ru/dev/metrika/ru/management/openapi/schema/uploadSimpleOrders) |
| Отправка подробных данных | [schema_getOrderStatuses](https://yandex.ru/dev/metrika/ru/management/openapi/schema/getOrderStatuses) | [schema_mapOrderStatuses](https://yandex.ru/dev/metrika/ru/management/openapi/schema/mapOrderStatuses) |
| Загрузка данных о клиентах | — | [schema_uploadContacts](https://yandex.ru/dev/metrika/ru/management/openapi/schema/uploadContacts), [schema_uploadContactJson_1](https://yandex.ru/dev/metrika/ru/management/openapi/schema/uploadContactJson_1) |
| Загрузка данных о заказах | — | [schema_uploadOrders](https://yandex.ru/dev/metrika/ru/management/openapi/schema/uploadOrders), [schema_uploadOrdersJson_1](https://yandex.ru/dev/metrika/ru/management/openapi/schema/uploadOrdersJson_1) |
| Загрузка пользовательских данных | [schema_getAttributes](https://yandex.ru/dev/metrika/ru/management/openapi/schema/getAttributes), [schema_getPredefinedTypes](https://yandex.ru/dev/metrika/ru/management/openapi/schema/getPredefinedTypes), [schema_getTypes](https://yandex.ru/dev/metrika/ru/management/openapi/schema/getTypes) | [schema_createAttributes](https://yandex.ru/dev/metrika/ru/management/openapi/schema/createAttributes), [schema_createProducts](https://yandex.ru/dev/metrika/ru/management/openapi/schema/createProducts) |
| Импорт данных о клиентах и заказах из CRM | [schema_getLastUploadings](https://yandex.ru/dev/metrika/ru/management/openapi/schema/getLastUploadings) | — |
| Импорт звонков | [call_findAllCallUploadings](https://yandex.ru/dev/metrika/ru/management/openapi/call/findAllCallUploadings), [call_findCallUploadingById](https://yandex.ru/dev/metrika/ru/management/openapi/call/findCallUploadingById) | [call_uploadCalls](https://yandex.ru/dev/metrika/ru/management/openapi/call/uploadCalls) |
| Импорт офлайн-конверсий | [offline_conversions_findAll_1](https://yandex.ru/dev/metrika/ru/management/openapi/offline_conversions/findAll_1), [offline_conversions_findById_1](https://yandex.ru/dev/metrika/ru/management/openapi/offline_conversions/findById_1) | [offline_conversions_upload_1](https://yandex.ru/dev/metrika/ru/management/openapi/offline_conversions/upload_1) |
| Импорт чатов | [chats_findAll_chats](https://yandex.ru/dev/metrika/ru/management/openapi/chats/findAll_chats), [chats_findById_chats](https://yandex.ru/dev/metrika/ru/management/openapi/chats/findById_chats) | [chats_upload_chats](https://yandex.ru/dev/metrika/ru/management/openapi/chats/upload_chats) |
| Импорт параметров посетителей | [user_params_findById](https://yandex.ru/dev/metrika/ru/management/openapi/user_params/findById), [user_params_findAll](https://yandex.ru/dev/metrika/ru/management/openapi/user_params/findAll) | [user_params_upload](https://yandex.ru/dev/metrika/ru/management/openapi/user_params/upload), [user_params_confirm](https://yandex.ru/dev/metrika/ru/management/openapi/user_params/confirm) |
| Measurement Protocol | — | [measurement_generateToken](https://yandex.ru/dev/metrika/ru/management/openapi/measurement/generateToken), [measurement_deleteToken](https://yandex.ru/dev/metrika/ru/management/openapi/measurement/deleteToken) |

### API отчетов

| Группа | R | W |
|---|---|---|
| Справочник API отчетов | [pivot](https://yandex.ru/dev/metrika/ru/stat/openapi/pivot), [drilldown](https://yandex.ru/dev/metrika/ru/stat/openapi/drilldown), [comparison_drilldown](https://yandex.ru/dev/metrika/ru/stat/openapi/comparison_drilldown), [comparison_1](https://yandex.ru/dev/metrika/ru/stat/openapi/comparison_1), [bytime](https://yandex.ru/dev/metrika/ru/stat/openapi/bytime), [data_1](https://yandex.ru/dev/metrika/ru/stat/openapi/data_1) | — |

### API Logs

| Группа | R | W |
|---|---|---|
| Справочник API Logs | [getLogRequests_1](https://yandex.ru/dev/metrika/ru/logs/openapi/getLogRequests_1), [evaluate](https://yandex.ru/dev/metrika/ru/logs/openapi/evaluate), [download](https://yandex.ru/dev/metrika/ru/logs/openapi/download), [getLogRequest](https://yandex.ru/dev/metrika/ru/logs/openapi/getLogRequest) | [createLogRequest_1](https://yandex.ru/dev/metrika/ru/logs/openapi/createLogRequest_1), [clean](https://yandex.ru/dev/metrika/ru/logs/openapi/clean), [cancel](https://yandex.ru/dev/metrika/ru/logs/openapi/cancel) |

### Measurement Protocol

| Группа | R | W |
|---|---|---|
| Загрузка взаимодействий | — | [collect_get](https://yandex.ru/dev/metrika/ru/data-import/measurement-upload), [collect_post](https://yandex.ru/dev/metrika/ru/data-import/measurement-upload) |

## Границы проверки

Проверены все 108 ссылок операций текущего оглавления: каждая прочитана, из неё извлечены HTTP-метод и точный URL. Все 110 ID уникальны; origin, эффекты GET с записью, отдельный Measurement Protocol и селектор чатов проверены отдельно. Живые изменения, загрузки CRM/звонков/чатов/расходов, новые Logs jobs и Measurement Protocol здесь не выполнялись.

Старый совместимый с Google Analytics API упоминается в [архитектуре](https://yandex.ru/dev/metrika/ru/intro/method-call), но отсутствует в текущем оглавлении операций. Его работоспособность и полный исторический состав не установлены; он не включён как подтверждённо действующий маршрут. Каталог также не является перечнем каждой метрики/группировки: они выбираются по [официальному справочнику](https://yandex.ru/dev/metrika/ru/stat/attrandmetr/dim_all), без собственного урезанного белого списка.
