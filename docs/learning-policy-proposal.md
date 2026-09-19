# Проверяемое обучение

Урок — проектная карточка, а не изменение весов модели или SKILL.md.
Новая карточка проходит candidate → independent evaluation → active version.
Нет критериев/источника/подтверждённого бизнес-исхода — INSUFFICIENT_DATA;
противоречие — OWNER_REQUIRED. Регрессия или провал защитного случая запрещают
активацию. Порог не уменьшается автоматически ради принятия урока.

Начальные предлагаемые критерии: минимум два независимых случая, хотя бы один
обязательный защитный; ни одной регрессии относительно базы; минимум одно новое
правильное решение. Это предложение рубрики, не доказанная достаточная выборка
для рекламного эффекта. Бизнес-утверждение требует отдельного evidence исхода.
Для реального домена пользователь уточняет объём/метрики, применимость и источники.

CLI: `lesson propose --input candidate.json`, `lesson status --candidate-id ID`.
Контракт кандидата содержит schema_version=1, project_id, context_hash,
base_version (context knowledge.lessons_version), methods_version (knowledge.version),
scope=project, domain=diagnosis/demand/ads, rule, applicability, exceptions,
sources (HTTPS без credentials), evidence_ids, training_case_ids,
claim=method/business_effect, outcome_evidence_id или null, conflicts.
Первый `lesson` инициализирует пустую версию. Получите свежий context после этого,
прежде чем заполнять candidate. Начните с `lesson init`; создание пустой версии не активирует ни одного урока.

`lesson evaluate --candidate-id ID --input results.json` читает только локальную
`projects/<id>/learning-policy.json`: `{schema_version:1, approved:true, criteria:...}`.
В поставке этого утверждённого файла нет. criteria: version, approval_source,
cases, min_improvement, sources_verified, outcome_verified. Случай содержит
id/input/expected/critical; обучающие ID не могут входить в контрольный набор.
Сохранённые ответы должны происходить из независимого прогона, не быть оценками
самого автора правила. Результаты: candidate_id, base_version, criteria_hash
(SHA-256 canonical JSON критериев), answers: `{case_id:{baseline:...,candidate:...}}`.
Код сравнивает ответы с expected, проверяет регрессии и выполняет активацию
автоматически после прохождения. Внутренний evaluator получает input/id без expected.

Оценивание содержания/источников выполняется отдельно: sources_verified и
outcome_verified — результат ответственного ревью, не утверждения из карточки.
Импорт файла не доказывает честность происхождения ответов. Workspace принадлежит
пользователю, поэтому подмена критериев/ответов процессом с shell технически
возможна. Не выдавайте эту схему за изолированную систему самообучения.

Активация использует compare-and-swap: если изменилась база/методика, STALE_BASE
и повторная оценка. Новая версия и событие сохраняются в одной SQLite-транзакции.
`context` возвращает активные уроки и общий knowledge.version; старые предложения
после смены знания требуют проверки. Код, политика и skill-файлы не меняются.
Клиентский урок не становится общим автоматически.

Откат: `lesson rollback --version OLD --expected-head CURRENT`. Разрешён только
предок активной версии своего проекта; история не стирается. Расход и рекламные
изменения откатом знания не отменяются.

ModelTask v1 принимает project_id, kind=intent-classification, text до 2000 символов,
timeout_seconds и schema_version. Два локальных stub проверяют одинаковый выход;
ошибка/невалидный ответ/таймаут возвращают abstain/error. Отдельный дочерний процесс
останавливается по timeout. Это не вызов AI Studio и не оценка качества реальной
модели. Текст предварительно минимизировать: строгие поля не заменяют DLP.
