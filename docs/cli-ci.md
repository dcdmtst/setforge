# CLI, CI и контракт результата

CLI печатает JSON в stdout, ошибки — в stderr. Коды: `0` — операция выполнена;
`2` — ошибка входов/конфигурации; `3` — ожидание либо неполный результат;
`130` — прерывание. **Код0 у claim/deliver не означает приёмку заказа.**

```text
setforge init --project .setforge/project --style-pack PACK
setforge run --project .setforge/project --brief examples/brief.json --run .setforge/runs/order-001
setforge status --run .setforge/runs/order-001
setforge queue --run .setforge/runs/order-001
setforge coordinate --run .setforge/runs/order-001
setforge result-manifest --run .setforge/runs/order-001
setforge export --run .setforge/runs/order-001
```

`run` создаёт заказ один раз; для продолжения используйте `coordinate`.
В файловом режиме активные внешние исполнители выполняют задания через
claim/deliver. Координатор обрабатывает входящие ответы. Протокол и восстановление:
[parallel-workers.md](parallel-workers.md).

В каталоге заказа появятся `result-manifest.json` и, **только при полной приёмке**,
`delivery.zip`. Внутри ZIP: автономный `report.html`, принятые исходники в
`images/`, игровые PNG в `cards/`, манифесты, проверка подписей, тайминги и Gantt.
Исходные эталоны, отклонённые версии и приватные задания туда не входят.
`export --out PATH.zip` задаёт другой путь архива. Каноническая веб-кнопка
скачивает `RUN/delivery.zip`.

`result-manifest.json` — version1, машинный контракт:

```json
{
  "schema_version": 1,
  "status": "complete",
  "full_delivery_ready": true,
  "requested_variants": 3,
  "expected_cards": 30,
  "accepted_cards": 30,
  "threshold": 93.5,
  "variants": [
    {"id": "v1", "title": "Game Night", "cards": [
      {"id": "dice", "title": "Dice", "stars": 1, "golden": false,
       "category": 1, "accepted": true, "quality": 95,
       "native_path": "images/v1/01-dice.png", "path": "cards/v1/01-dice.png",
       "source_sha256": "HASH_OF_NATIVE", "sha256": "HASH_OF_CARD"}
    ]}
  ]
}
```

Это сокращённая иллюстрация полей, не настоящий результат. В реальной выдаче
обязательны все наборы и карточки; хеши содержат SHA256 файлов. Потребитель
требует `full_delivery_ready:true`, затем сверяет файлы. Для неполного заказа
манифест сохранится с false, команда вернёт3. Проверяются полнота, числовая
приёмка, метаданные и хеши.

`pack --run RUN --out PRIVATE.zip` — **другая операция**: полный локальный бэкап
с историей и эталонами. Его нельзя публиковать или считать принятой выдачей.

## CI/CD

GitHub workflow запускает только офлайн-тесты, сборку и проверку исходников.
Он не обращается к моделям и не загружает артефакты экспериментов.

Производственный runner: установите пакет, смонтируйте локальный style pack,
передайте JSON-бриф, запустите координатор и внешних workers. Среда должна
предоставлять генератор и независимого ревьюера. Встроенный imagegen чата не
считается доступным в GitHub Actions. Командные/API workers работают отдельно,
вне блокировки координатора. После их завершения проверьте `result-manifest`,
выполните `export` и заберите файлы. Ключи — через секреты окружения, никогда
через бриф или исходный репозиторий.

Полезные команды: `schema production-plan`, `schema production-card-review`,
`schema entity-review`, `prepare-job`, `validate-response`, `allocation-check`,
`image-start`, `image-finish`, `nameplate-check`, `acceptance-policy`,
`inbox --runs RUNS --wait-seconds 30`.
Файловый исполнитель встроенного генератора использует image-start/image-finish
вокруг одного вызова. Подбор референсов, UTC-метки, хеш PNG и готовый ответ
записываются кодом: [image-execution.md](image-execution.md).
