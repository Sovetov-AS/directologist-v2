# Каталог публичного API Яндекс Директа

Проверено 12 ноября 2026 года по официальной документации и анонимному чтению публичных WSDL/XSD. Каталог: **23 текущих RPC-сервиса, 87 методов (27 без изменения объектов, 60 изменяющих), отдельно Reports**. Ещё четыре снятых операции учтены отдельно. Составление этого каталога не требовало доступа к аккаунту; последующие реальные проверки чтения описаны отдельно в [подключении API](yandex-api.md) и [verification.json](../work/yandex-connection/verification.json). Рекламные объекты не изменялись.

Машиночитаемая версия — [yandex-direct-methods.json](../config/yandex-direct-methods.json). Это каталог возможностей провайдера, а не разрешение на выполнение. В JSON `services.Reports.methods.get` — виртуальное имя операции клиента: реального RPC-метода `Reports.get` у провайдера нет.

## Что считается полным

Проверено текущее русское [оглавление справочника](https://yandex.ru/dev/direct/doc/ru/adextensions/adextensions), включая разворачиваемый раздел устаревших методов, страницы всех текущих сервисов и методов, а также их публичные схемы. Это полнота **на уровне имён текущих сервисов/методов данного справочника**, не всех полей, типов, исторических версий или функций кабинета. Краткий [обзор справочника](https://yandex.ru/dev/direct/doc/ru/concepts/about) не использован как единственный источник: он отстаёт от оглавления.

Выявлены расхождения: тело страницы AgencyClients всё ещё перечисляет снятый add, хотя текущие методы — addPassportOrganization и addPassportOrganizationMember; тело Dictionaries пропускает getGeoRegions. В каталоге использованы отдельные страницы методов и актуальное оглавление.

## Версии и транспорт

Для каждого из 23 текущих сервисов анонимные GET публичных `https://api.direct.yandex.com/v5/<service>?wsdl` и `.../v501/<service>?wsdl` вернули HTTP 200 и схемы с перечисленными методами. В обеих схемах AgencyClients дополнительно сохранён снятый add. Доступность схем **не доказывает** работоспособность снятых операций, наличие прав, равенство всех полей или поддержку каждого типа объектов. В JSON у каждого сервиса сохранены адреса этой проверки.

Текущие страницы сервисов указывают `/json/v501/<service>`; в каталоге это версия по умолчанию. Пример [JSON-транспорта](https://yandex.ru/dev/direct/doc/ru/concepts/json) всё ещё использует `/json/v5/ads/`. Для единой перфоманс-группы документация [AdGroups.add](https://yandex.ru/dev/direct/doc/ru/adgroups/add) явно указывает v501; для товарных объявлений аналогичное требование есть в [Ads.add](https://yandex.ru/dev/direct/doc/ru/ads/add). Нельзя выбирать v5 для новых типов только потому, что имя метода присутствует в его WSDL.

Обычные сервисы используют HTTPS POST, UTF-8 и JSON-объект с `method` и, как правило, `params`; поддерживается также SOAP. HTTP 200 нужно разбирать: в теле возможна общая ошибка, а в пакетах — результаты и ошибки отдельных объектов. Источники: [формат](https://yandex.ru/dev/direct/doc/ru/concepts/format), [JSON](https://yandex.ru/dev/direct/doc/ru/concepts/json).

Заголовки запросов: Authorization, Accept-Language, Client-Login для применимого клиентского контекста агентства, Use-Operator-Units для расходования баллов агентства, Accept-Encoding. Сохраняемые заголовки ответа: RequestId, Units, Units-Used-Login. [Документация заголовков](https://yandex.ru/dev/direct/doc/ru/concepts/headers). У AgencyClients Client-Login не передают; для [addPassportOrganization](https://yandex.ru/dev/direct/doc/ru/agencyclients/addPassportOrganization) дополнительно нужен Member-Authorization. Секретные заголовки не должны попадать в журнал.

## Текущие сервисы и методы

R — не меняет рекламные/пользовательские объекты. W — создаёт/изменяет/удаляет объекты, меняет ставки, показы, модерацию или участников. R может расходовать баллы; вычисление результата также отнесено к R. Неизвестные методы автоматически к R не относятся. Все строки имеют публичные схемы v5 и v501 с оговорками выше.

| Сервис | Методы R | Методы W |
|---|---|---|
| [AdExtensions](https://yandex.ru/dev/direct/doc/ru/adextensions/adextensions) | [get](https://yandex.ru/dev/direct/doc/ru/adextensions/get) | [add](https://yandex.ru/dev/direct/doc/ru/adextensions/add), [delete](https://yandex.ru/dev/direct/doc/ru/adextensions/delete) |
| [AdGroups](https://yandex.ru/dev/direct/doc/ru/adgroups/adgroups) | [get](https://yandex.ru/dev/direct/doc/ru/adgroups/get) | [add](https://yandex.ru/dev/direct/doc/ru/adgroups/add), [delete](https://yandex.ru/dev/direct/doc/ru/adgroups/delete), [update](https://yandex.ru/dev/direct/doc/ru/adgroups/update) |
| [AdImages](https://yandex.ru/dev/direct/doc/ru/adimages/adimages) | [get](https://yandex.ru/dev/direct/doc/ru/adimages/get) | [add](https://yandex.ru/dev/direct/doc/ru/adimages/add), [delete](https://yandex.ru/dev/direct/doc/ru/adimages/delete) |
| [Ads](https://yandex.ru/dev/direct/doc/ru/ads/ads) | [get](https://yandex.ru/dev/direct/doc/ru/ads/get) | [add](https://yandex.ru/dev/direct/doc/ru/ads/add), [archive](https://yandex.ru/dev/direct/doc/ru/ads/archive), [delete](https://yandex.ru/dev/direct/doc/ru/ads/delete), [moderate](https://yandex.ru/dev/direct/doc/ru/ads/moderate), [resume](https://yandex.ru/dev/direct/doc/ru/ads/resume), [suspend](https://yandex.ru/dev/direct/doc/ru/ads/suspend), [unarchive](https://yandex.ru/dev/direct/doc/ru/ads/unarchive), [update](https://yandex.ru/dev/direct/doc/ru/ads/update) |
| [AdVideos](https://yandex.ru/dev/direct/doc/ru/advideos/advideos) | [get](https://yandex.ru/dev/direct/doc/ru/advideos/get) | [add](https://yandex.ru/dev/direct/doc/ru/advideos/add) |
| [AgencyClients](https://yandex.ru/dev/direct/doc/ru/agencyclients/agencyclients) | [get](https://yandex.ru/dev/direct/doc/ru/agencyclients/get) | [addPassportOrganization](https://yandex.ru/dev/direct/doc/ru/agencyclients/addPassportOrganization), [addPassportOrganizationMember](https://yandex.ru/dev/direct/doc/ru/agencyclients/addPassportOrganizationMember), [update](https://yandex.ru/dev/direct/doc/ru/agencyclients/update) |
| [AudienceTargets](https://yandex.ru/dev/direct/doc/ru/audiencetargets/audiencetargets) | [get](https://yandex.ru/dev/direct/doc/ru/audiencetargets/get) | [add](https://yandex.ru/dev/direct/doc/ru/audiencetargets/add), [delete](https://yandex.ru/dev/direct/doc/ru/audiencetargets/delete), [resume](https://yandex.ru/dev/direct/doc/ru/audiencetargets/resume), [setBids](https://yandex.ru/dev/direct/doc/ru/audiencetargets/setBids), [suspend](https://yandex.ru/dev/direct/doc/ru/audiencetargets/suspend) |
| [Bids](https://yandex.ru/dev/direct/doc/ru/bids/bids) | [get](https://yandex.ru/dev/direct/doc/ru/bids/get) | [set](https://yandex.ru/dev/direct/doc/ru/bids/set), [setAuto](https://yandex.ru/dev/direct/doc/ru/bids/setAuto) |
| [Businesses](https://yandex.ru/dev/direct/doc/ru/businesses/businesses) | [get](https://yandex.ru/dev/direct/doc/ru/businesses/get) | — |
| [BidModifiers](https://yandex.ru/dev/direct/doc/ru/bidmodifiers/bidmodifiers) | [get](https://yandex.ru/dev/direct/doc/ru/bidmodifiers/get) | [add](https://yandex.ru/dev/direct/doc/ru/bidmodifiers/add), [delete](https://yandex.ru/dev/direct/doc/ru/bidmodifiers/delete), [set](https://yandex.ru/dev/direct/doc/ru/bidmodifiers/set) |
| [Campaigns](https://yandex.ru/dev/direct/doc/ru/campaigns/campaigns) | [get](https://yandex.ru/dev/direct/doc/ru/campaigns/get) | [add](https://yandex.ru/dev/direct/doc/ru/campaigns/add), [archive](https://yandex.ru/dev/direct/doc/ru/campaigns/archive), [delete](https://yandex.ru/dev/direct/doc/ru/campaigns/delete), [resume](https://yandex.ru/dev/direct/doc/ru/campaigns/resume), [suspend](https://yandex.ru/dev/direct/doc/ru/campaigns/suspend), [unarchive](https://yandex.ru/dev/direct/doc/ru/campaigns/unarchive), [update](https://yandex.ru/dev/direct/doc/ru/campaigns/update) |
| [Changes](https://yandex.ru/dev/direct/doc/ru/changes/changes) | [checkDictionaries](https://yandex.ru/dev/direct/doc/ru/changes/checkDictionaries), [checkCampaigns](https://yandex.ru/dev/direct/doc/ru/changes/checkCampaigns), [check](https://yandex.ru/dev/direct/doc/ru/changes/check) | — |
| [Clients](https://yandex.ru/dev/direct/doc/ru/clients/clients) | [get](https://yandex.ru/dev/direct/doc/ru/clients/get) | [update](https://yandex.ru/dev/direct/doc/ru/clients/update) |
| [Creatives](https://yandex.ru/dev/direct/doc/ru/creatives/creatives) | [get](https://yandex.ru/dev/direct/doc/ru/creatives/get) | [add](https://yandex.ru/dev/direct/doc/ru/creatives/add) |
| [Dictionaries](https://yandex.ru/dev/direct/doc/ru/dictionaries/dictionaries) | [get](https://yandex.ru/dev/direct/doc/ru/dictionaries/get), [getGeoRegions](https://yandex.ru/dev/direct/doc/ru/dictionaries/getGeoRegions) | — |
| [Feeds](https://yandex.ru/dev/direct/doc/ru/feeds/feeds) | [get](https://yandex.ru/dev/direct/doc/ru/feeds/get) | [add](https://yandex.ru/dev/direct/doc/ru/feeds/add), [delete](https://yandex.ru/dev/direct/doc/ru/feeds/delete), [update](https://yandex.ru/dev/direct/doc/ru/feeds/update) |
| [KeywordBids](https://yandex.ru/dev/direct/doc/ru/keywordbids/keywordbids) | [get](https://yandex.ru/dev/direct/doc/ru/keywordbids/get) | [set](https://yandex.ru/dev/direct/doc/ru/keywordbids/set), [setAuto](https://yandex.ru/dev/direct/doc/ru/keywordbids/setAuto) |
| [Keywords](https://yandex.ru/dev/direct/doc/ru/keywords/keywords) | [get](https://yandex.ru/dev/direct/doc/ru/keywords/get) | [add](https://yandex.ru/dev/direct/doc/ru/keywords/add), [delete](https://yandex.ru/dev/direct/doc/ru/keywords/delete), [resume](https://yandex.ru/dev/direct/doc/ru/keywords/resume), [suspend](https://yandex.ru/dev/direct/doc/ru/keywords/suspend), [update](https://yandex.ru/dev/direct/doc/ru/keywords/update) |
| [KeywordsResearch](https://yandex.ru/dev/direct/doc/ru/keywordsresearch/keywordsresearch) | [deduplicate](https://yandex.ru/dev/direct/doc/ru/keywordsresearch/deduplicate), [hasSearchVolume](https://yandex.ru/dev/direct/doc/ru/keywordsresearch/hasSearchVolume) | — |
| [NegativeKeywordSharedSets](https://yandex.ru/dev/direct/doc/ru/negativekeywordsharedsets/negativekeywordsharedsets) | [get](https://yandex.ru/dev/direct/doc/ru/negativekeywordsharedsets/get) | [add](https://yandex.ru/dev/direct/doc/ru/negativekeywordsharedsets/add), [delete](https://yandex.ru/dev/direct/doc/ru/negativekeywordsharedsets/delete), [update](https://yandex.ru/dev/direct/doc/ru/negativekeywordsharedsets/update) |
| [RetargetingLists](https://yandex.ru/dev/direct/doc/ru/retargetinglists/retargetinglists) | [get](https://yandex.ru/dev/direct/doc/ru/retargetinglists/get) | [add](https://yandex.ru/dev/direct/doc/ru/retargetinglists/add), [delete](https://yandex.ru/dev/direct/doc/ru/retargetinglists/delete), [update](https://yandex.ru/dev/direct/doc/ru/retargetinglists/update) |
| [Sitelinks](https://yandex.ru/dev/direct/doc/ru/sitelinks/sitelinks) | [get](https://yandex.ru/dev/direct/doc/ru/sitelinks/get) | [add](https://yandex.ru/dev/direct/doc/ru/sitelinks/add), [delete](https://yandex.ru/dev/direct/doc/ru/sitelinks/delete) |
| [Strategies](https://yandex.ru/dev/direct/doc/ru/strategies/strategies) | [get](https://yandex.ru/dev/direct/doc/ru/strategies/get) | [add](https://yandex.ru/dev/direct/doc/ru/strategies/add), [archive](https://yandex.ru/dev/direct/doc/ru/strategies/archive), [unarchive](https://yandex.ru/dev/direct/doc/ru/strategies/unarchive), [update](https://yandex.ru/dev/direct/doc/ru/strategies/update) |

Методы [Changes.check](https://yandex.ru/dev/direct/doc/ru/changes/check), [checkCampaigns](https://yandex.ru/dev/direct/doc/ru/changes/checkCampaigns) и [checkDictionaries](https://yandex.ru/dev/direct/doc/ru/changes/checkDictionaries) сообщают об уже произошедших изменениях; последний также возвращает время сервера. Они не запускают изменения.

[KeywordsResearch.deduplicate](https://yandex.ru/dev/direct/doc/ru/keywordsresearch/deduplicate) обрабатывает переданный массив и **возвращает инструкции**, что добавить, изменить и удалить. Само применение — отдельные вызовы Keywords.add/update/delete. Поэтому deduplicate классифицирован как R. [hasSearchVolume](https://yandex.ru/dev/direct/doc/ru/keywordsresearch/hasSearchVolume) даёт предварительный прогноз наличия показов, а не выгрузку всего спроса Wordstat.

## Reports — отдельный протокол

[Reports](https://yandex.ru/dev/direct/doc/ru/reports) принимает POST с JSON-обёрткой `params` по [спецификации](https://yandex.ru/dev/direct/doc/ru/spec), **без `method`**, и возвращает отчёт TSV в UTF-8. Есть также XML-вариант. Документированный JSON-адрес — `https://api.direct.yandex.com/json/v501/reports`. Обе публичные XSD (`/v5/reports.xsd`, `/v501/reports.xsd`) успешно прочитаны; это не проверка выполнения отчёта.

В [перечне типов](https://yandex.ru/dev/direct/doc/ru/type) восемь значений: ACCOUNT_PERFORMANCE_REPORT, CAMPAIGN_PERFORMANCE_REPORT, ADGROUP_PERFORMANCE_REPORT, AD_PERFORMANCE_REPORT, CRITERIA_PERFORMANCE_REPORT, CUSTOM_REPORT, REACH_AND_FREQUENCY_PERFORMANCE_REPORT, SEARCH_QUERY_PERFORMANCE_REPORT. Допустимые поля, фильтры и группировки зависят от типа.

Режим задаётся processingMode: auto, online или offline. HTTP 200 означает готовый отчёт, 201 — поставлен в очередь, 202 — формируется. Проверка готовности повторяет **тот же отчётный запрос**, учитывая retryIn. SEARCH_QUERY_PERFORMANCE_REPORT доступен только offline. Это правило опроса не является разрешением повторять рекламные записи с неизвестным исходом. [Режимы отчётов](https://yandex.ru/dev/direct/doc/ru/mode).

Дополнительные [заголовки Reports](https://yandex.ru/dev/direct/doc/ru/headers): returnMoneyInMicros, skipReportHeader, skipColumnHeader, skipReportSummary, Accept-Encoding; ответы содержат retryIn и reportsInQueue. Если returnMoneyInMicros:false отсутствует, денежные значения передаются в миллионных долях валюты. Создание временной задачи отчёта не меняет рекламные объекты.

## Реальные ограничения провайдера

- Общий предел — пять одновременных запросов на рекламодателя. Суточный лимит баллов индивидуален; выдаётся по часовым долям в скользящем окне. Ошибки тоже могут расходовать баллы. Нельзя подменять его произвольным постоянным бюджетом инструмента. [Ограничения и баллы](https://yandex.ru/dev/direct/doc/ru/concepts/units).
- Reports: до 20 запросов за 10 секунд на пользователя, до пяти ожидающих офлайн-отчётов, хранение готового офлайн-отчёта пять часов. Общая история — три года от текущего месяца; Query — 180 дней. Данные Метрики могут задерживаться и требуют корректной настройки счётчиков/целей. [Ограничения Reports](https://yandex.ru/dev/direct/doc/ru/restrictions).
- KeywordsResearch.hasSearchVolume: до 20 запросов за 60 секунд на рекламодателя. [Источник](https://yandex.ru/dev/direct/doc/ru/keywordsresearch/hasSearchVolume).
- AgencyClients.addPassportOrganizationMember: до 10 приглашений в день на PassportOrganizationLogin. [Источник](https://yandex.ru/dev/direct/doc/ru/agencyclients/addPassportOrganizationMember).

Размеры пакетов зависят от метода. Проверенные примеры: Campaigns.add — 10 кампаний; AdGroups.add/update и Ads.add — 1000 объектов; Keywords.update — 1000; AdExtensions.add — 1000; AdImages.add — максимум 100 изображений, рекомендация Яндекса — не более трёх; AdVideos.add — 10 URL или одно бинарное видео; Creatives.add — 10 видео; Feeds.add — 50 фидов; Sitelinks.add — 1000 наборов. Источники прикреплены к соответствующим методам в таблице и JSON. Рекомендация не заменяет жёсткий лимит, а размер пакета не ограничивает объём всей задачи.

Этот каталог не воспроизводит все ограничения каждого поля. При конкретной операции нужны страница метода, актуальные Restrictions из [Clients.get](https://yandex.ru/dev/direct/doc/ru/clients/get) / [AgencyClients.get](https://yandex.ru/dev/direct/doc/ru/agencyclients/get), справочники [Dictionaries.get](https://yandex.ru/dev/direct/doc/ru/dictionaries/get), состояние объектов и доступы аккаунта. Пагинацию и LimitedBy следует обрабатывать по конкретному get; один ответ не всегда равен полному набору.

## Снятые операции

| Операция | Подтверждённый статус |
|---|---|
| [AgencyClients.add](https://yandex.ru/dev/direct/doc/ru/agencyclients/add) | Больше не поддерживается; замена addPassportOrganization. Changelog: 16.06.2025. |
| BidModifiers.toggle | Снята 13.11.2025 по [changelog](https://yandex.ru/dev/direct/doc/ru/changelog); отдельная страница перенаправляет на главную, текущие WSDL её не содержат. |
| [Leads.get](https://yandex.ru/dev/direct/doc/ru/leads/get) | Больше не поддерживается. |
| [TurboPages.get](https://yandex.ru/dev/direct/doc/ru/turbopages/get) | Больше не поддерживается, несмотря на нейтральное описание страницы сервиса. |

У Leads и TurboPages публичные схемы v5/v501 по-прежнему отвечают HTTP 200; это наглядное основание не приравнивать WSDL к рабочему методу. Снятые операции находятся только в `deprecated`, а не в текущем списке исполняемых методов.

## Границы API и проверки

Наличие универсального JSON-транспорта не обеспечивает весь функционал интерфейса Директа. Например, [Creatives.add](https://yandex.ru/dev/direct/doc/ru/creatives/add) создаёт именно креативы видеодополнений по ID AdVideos, а не любой вид креатива. Поддержка конкретного рекламного формата определяется его схемой и текущим поведением: старый TextAd нельзя считать универсальным способом создания новых объявлений. [Текущие правила Ads.add](https://yandex.ru/dev/direct/doc/ru/ads/add).

По [changelog](https://yandex.ru/dev/direct/doc/ru/changelog) песочница недоступна с 15.07.2026. Нельзя обещать доступный sandbox на основании старого адреса или навыка. Wordstat, управление целями Метрики и CRM не становятся функциями Direct API только из-за общих бизнес-задач.

Проверки: публичное оглавление сверено с перечнем; все 87 текущих имён методов найдены в WSDL обеих версий; отдельно прочитаны неоднозначные check/deduplicate и снятые методы; обе XSD Reports доступны; JSON разобран и проверены число методов, effect, адреса источников и отделение deprecated.

Сам каталог не доказывает выполнение методов в аккаунте, права токена и индивидуальные лимиты: результаты отдельных живых проверок см. в [подключении API](yandex-api.md). Не проверены равенство всех полей v5/v501, все UI-возможности и весь исторический API 4/Live 4. В старых материалах встречаются VCards, SmartAdTargets и другие снятые/исторические сущности; они не объявлены рабочими только по старым упоминаниям. Отсутствие в этом текущем каталоге не является исчерпывающим реестром истории API.
