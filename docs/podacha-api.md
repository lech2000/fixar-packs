# Путь А: подача целиком через API

Всё — вход, доверие, заявка издателя, подача версии и загрузка архива — через
`https://api.agrigate.pro`. Ниже — рабочий сценарий с `curl` от первого
запроса до появления пака в каталоге. Все ответы площадка присылает в JSON;
для чтения полей в примерах используется `jq` (не обязателен — можно читать
глазами).

Если вы предпочитаете не собирать команды `curl` руками, тот же результат на
шагах «заявить документы», «заявка издателя», «подать версию» и «отдать
архив» даёт кабинет автора: `https://agrigate.pro/pak/kabinet/`.

## 0. Проверить манифест заранее (необязательно, без входа)

`POST /registry/validate` гоняет ту же проверку, что и подача версии, но
ничего не подаёт и не занимает — можно звать сколько угодно раз, отвечает
всегда `200`, годность смотрят по полю `ok`.

```bash
jq -n --rawfile y packs/acme/inbox/pack.yaml '{pack_yaml: $y}' \
  | curl -s -X POST https://api.agrigate.pro/registry/validate \
      -H 'Content-Type: application/json' --data-binary @-
```

Годный манифест отвечает `{"ok": true, "kind": ..., "полное_имя": ..., "title": ..., "просит_прав": [...], "note": ...}`.
Негодный — `{"ok": false, "problems": [...], "hint": ...}`.

Тот же самый чек локально, без сети: `python3 tools/pack.py check packs/acme/inbox`
(поле `hint` в ответе площадки может называть путь к чужому внутреннему
инструменту — сверяйтесь с этой командой из данного репозитория, а не с
текстом `hint` дословно).

## 1. Войти без регистрации

```bash
curl -s -X POST https://api.agrigate.pro/entry/anon \
  -H 'Content-Type: application/json' \
  -d '{"label": "", "page_key": ""}'
```

Ответ содержит `principal_id`, `token` (Bearer-токен сессии) и `assurance: 0`.
Сохраните оба значения — они нужны на следующих шагах:

```bash
export FIXAR_TOKEN=<token из ответа>
export FIXAR_PRINCIPAL=<principal_id из ответа>
```

Уровень доверия `assurance=0` достаточен для `GET /trust/schema` и для
`POST /registry/validate`, но НЕ достаточен для заявления документов личности
или юрлица (нужен `assurance>=2`) — это следующий шаг.

Если площадка временно не принимает новых посетителей, вернётся `503` с
`reason: "new_visitors_paused"` — подождите и повторите позже.

## 2. Поднять доверие до VERIFIED (вход по телефону)

Вход по телефону завершается новым Bearer-токеном с `assurance=2`.

```bash
curl -s -X POST https://api.agrigate.pro/auth/phone/request \
  -H 'Content-Type: application/json' \
  -d '{"phone": "+7XXXXXXXXXX", "anon_token": "'"$FIXAR_TOKEN"'"}'
```

`anon_token` передаётся, чтобы дела анонимной сессии перенеслись на субъекта,
подтвердившего номер. Ответ несёт `expires_in`, `code_length`,
`attempts_allowed` — код приходит по СМС.

```bash
curl -s -X POST https://api.agrigate.pro/auth/phone/verify \
  -H 'Content-Type: application/json' \
  -d '{"phone": "+7XXXXXXXXXX", "code": "<код из СМС>"}'
```

Ответ несёт новый `token` и `assurance: 2` — обновите переменные:

```bash
export FIXAR_TOKEN=<новый token из ответа>
export FIXAR_PRINCIPAL=<principal_id из ответа>
```

Частые отказы: `422` — номер не похож на телефонный; `429` — код уже
отправлен (см. заголовок `Retry-After`) или лимит на сегодня; при проверке —
`404` код не запрашивался, `410` просрочен, `403` не подошёл (в сообщении
остаток попыток).

## 3. Посмотреть, что спросит проверка личности

```bash
curl -s "https://api.agrigate.pro/trust/schema?country=RU" \
  -H "Authorization: Bearer $FIXAR_TOKEN"
```

Ответ называет, какие реквизиты будут нужны на следующем шаге (для физлица
или для организации — по стране).

## 4. Заявить документы

Поле `principal_id` в теле дверь всё равно перезапишет вашим настоящим
субъектом — но по схеме оно обязано присутствовать, поэтому передаём то, что
получили на шаге 2.

Физическое лицо:

```bash
curl -s -X POST https://api.agrigate.pro/trust/verify/identity \
  -H "Authorization: Bearer $FIXAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "principal_id": "'"$FIXAR_PRINCIPAL"'",
    "full_name": "Фамилия Имя Отчество",
    "doc_number": "0000 000000",
    "doc_type": "passport",
    "country": "RU",
    "birth_date": "1990-01-01"
  }'
```

Организация (для России `legal_id2` — ОГРН — обязателен):

```bash
curl -s -X POST https://api.agrigate.pro/trust/verify/legal \
  -H "Authorization: Bearer $FIXAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "principal_id": "'"$FIXAR_PRINCIPAL"'",
    "legal_name": "ООО «Ромашка»",
    "country": "RU",
    "legal_id": "0000000000",
    "legal_id2": "0000000000000"
  }'
```

Ответ `status: approved` означает только «заявлено и не найдено в санкционных
списках» — подлинность документа этим не подтверждается. Настоящую сверку
позже делает вручную распорядитель площадки; API-пути к этому шагу нет,
только ожидание.

Без `assurance>=2` вернётся `403` с `reason: "assurance_required"` — значит,
шаг 2 не выполнен или токен устарел.

## 5. Заявить издателя и получить допуск

```bash
curl -s -X POST https://api.agrigate.pro/registry/publishers \
  -H "Authorization: Bearer $FIXAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "principal_id": "'"$FIXAR_PRINCIPAL"'",
    "namespace": "acme",
    "display_name": "Acme Ltd",
    "contact": "hello@acme.example"
  }'
```

Пространство имён закрепляется за вами сразу; допуск публиковать — отдельное
решение площадки, до 24 часов (поле `decide_by` в ответе), и оно возможно
только после того, как площадка сверит документы из шага 4. В `contact`
укажите, как с вами связаться: по этому контакту площадка попросит документы
для сверки. Копии документов площадка не хранит. Следить за
решением:

```bash
curl -s "https://api.agrigate.pro/registry/publishers/$FIXAR_PRINCIPAL" \
  -H "Authorization: Bearer $FIXAR_TOKEN"
```

Заявка на занятое пространство имён вернёт `409` («имя издателя занимается
навсегда»).

## 6. Подать версию манифеста

Ровно одно из `manifest` или `pack_yaml` — оба сразу или оба пустых дадут
`422`.

```bash
jq -n --rawfile y packs/acme/inbox/pack.yaml --arg pid "$FIXAR_PRINCIPAL" \
  '{principal_id: $pid, pack_yaml: $y}' \
  | curl -s -X POST https://api.agrigate.pro/registry/versions \
      -H "Authorization: Bearer $FIXAR_TOKEN" \
      -H 'Content-Type: application/json' --data-binary @-
```

Сохраните `id` из ответа — это `version_id`, он нужен для загрузки архива:

```bash
export FIXAR_VERSION_ID=<id из ответа>
```

Возможные отказы: `403` — издатель ещё не получил допуск, приостановлен, или
namespace манифеста не совпадает с закреплённым за вами; `409` — версия с
этим номером уже подана (с тем же содержимым или с другим — опубликованная
версия неизменяема, нужен новый номер).

## 7. Собрать и отдать архив

Сначала собрать архив локально — детерминированно, с отпечатком:

```bash
python3 tools/pack.py build packs/acme/inbox --out dist
```

Печатает одну строку JSON вида
`{"file": "dist/acme-inbox-1.0.0.tar.gz", "sha256": "...", "size": N, ...}`.
Сохраните `sha256` и `size`:

```bash
export FIXAR_SHA256=<sha256 из вывода>
export FIXAR_SIZE=<size из вывода>
```

Запросить одноразовый адрес карантинной корзины:

```bash
curl -s -X POST https://api.agrigate.pro/registry/artifacts/upload-url \
  -H "Authorization: Bearer $FIXAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "principal_id": "'"$FIXAR_PRINCIPAL"'",
    "version_id": "'"$FIXAR_VERSION_ID"'",
    "declared_digest": "'"$FIXAR_SHA256"'",
    "size_bytes": '"$FIXAR_SIZE"'
  }'
```

```bash
export FIXAR_UPLOAD_URL=<upload_url из ответа>
```

Загрузить байты — этот запрос идёт прямо в хранилище, не через
`api.agrigate.pro`, и у него нет отдельного маршрута гейтвея площадки:

```bash
curl -s -X PUT "$FIXAR_UPLOAD_URL" --data-binary @dist/acme-inbox-1.0.0.tar.gz
```

Адрес одноразовый и ограничен по времени (по умолчанию час) — если не
успели, запросите новый `upload-url`.

Завершить приём — площадка сверит настоящий отпечаток загруженных байт с
заявленным ДО того, как прочитает содержимое, и только при совпадении отдаст
архив сканеру:

```bash
curl -s -X POST https://api.agrigate.pro/registry/artifacts/finalize \
  -H "Authorization: Bearer $FIXAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "principal_id": "'"$FIXAR_PRINCIPAL"'",
    "version_id": "'"$FIXAR_VERSION_ID"'"
  }'
```

Ответ несёт состояние артефакта (`scan_passed`/`scan_failed`) и
`scan_report.findings` со всем, что нашёл сканер. Отпечаток неизменяем: если
на этом шаге он не сошёлся, состояние `scan_failed` записывается навсегда для
этой версии — поправить нельзя, только подать новую версию.

## 8. Следить за решением

```bash
curl -s "https://api.agrigate.pro/registry/publishers/$FIXAR_PRINCIPAL/versions" \
  -H "Authorization: Bearer $FIXAR_TOKEN"
```

и ящик уведомлений (виды `pack.publisher`, `pack.version`):

```bash
curl -s "https://api.agrigate.pro/principals/$FIXAR_PRINCIPAL/notifications?unread_only=true" \
  -H "Authorization: Bearer $FIXAR_TOKEN"
```

Одобрят только версию с продвинутым площадкой архивом — прошедшего сканер
(`scan_passed`) недостаточно, продвижение делает площадка отдельным решением.
Срок разбора версии — до 72 часов от подачи (`decide_by` в ответе шага 6).

## 9. При необходимости — отозвать версию

Отзыв запрещает НОВЫЕ установки; у кого версия уже стоит — не трогается.

```bash
curl -s -X POST "https://api.agrigate.pro/registry/versions/$FIXAR_VERSION_ID/yank" \
  -H "Authorization: Bearer $FIXAR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "principal_id": "'"$FIXAR_PRINCIPAL"'",
    "note": "нашли ошибку в шаблоне дела, готовим новую версию"
  }'
```

## На что обратить внимание

- Все поля в телах запросов заданы строго (`extra: forbid`) — лишнее поле
  даёт `422` ещё до бизнес-логики.
- `principal_id`, который вы указываете в теле, всегда перезаписывается вашим
  настоящим субъектом — указывать чужой смысла нет, но поле всё равно должно
  присутствовать по схеме.
- Отпечаток версии и артефакта неизменяемы: другое содержимое под тем же
  номером версии — это `409`, лечится только новым номером версии.
