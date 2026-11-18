# Локальный пакет эталонов

Подготовьте папку с файлами `style-pack.json`, `style.md` и вашими PNG/JPEG/WebP.
Используйте изображения, которые имеете право передавать выбранному генератору.
Они копируются в локальный проект и снимок заказа; на GitHub не отправляются.

Пример `style-pack.json` (пути относительно папки пакета):

```json
{
  "version": 1,
  "id": "family-collection",
  "style": "style.md",
  "references": [
    {"id": "simple-prop", "path": "simple-prop.png", "background": 1,
     "description": "One floating prop; matte materials and soft volume"},
    {"id": "surface-prop", "path": "surface-prop.png", "background": 3,
     "description": "Readable surface treatment without dense microtexture"},
    {"id": "family-scene", "path": "family-scene.png", "background": 4,
     "description": "Two characters integrated into a place"}
  ]
}
```

В `style.md` опишите целевой стиль и частые ошибки: формы, цвет, свет,
материалы, фон, границы стилизации. Желательно покрыть разные категории,
предметы и сценки. Три файла выше — пример структуры, а не готовые эталоны.
Можно добавить весь свой каталог; первыми тремя задаются общие референсы.
Дополнительные референсы выбираются по задачам.

```text
setforge init --project .setforge/project --style-pack PACK
```

Если проект уже создан:

```text
setforge import-style --project .setforge/project --style-pack PACK
setforge doctor --project .setforge/project
```

Импорт влияет на будущие заказы; входы существующих заказов остаются прежними.
Не меняйте проект во время создания нового заказа. IDs должны быть уникальными
строчными латинскими идентификаторами. Выход за папку пакета запрещён.
Если референсов нет или их хеши изменились, производство блокируется.
