"""Offline previews, verified exports and experiment history."""

import csv
import html
import math
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .io import event, inside, read, sha, write
from .acceptance import score as art_score
from .allocation import require_gate
from .naming import names, display_title
from .showcase import acceptance_text, discard_reason, qualified_entities


def font(size=18):
    for name in ("C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def contact_sheets(run, state, config, production_only=False):
    run = Path(run)
    target = run / "previews"
    target.mkdir(exist_ok=True)
    paths = []
    mapping = names(run)
    for variant in (state["plan"] or {}).get("variants", []):
        if production_only and state.get('production_contract',{}).get('mode')!='full' and state.get("production_variant_ids") and variant["id"] not in state["production_variant_ids"]:
            continue
        sheet = Image.new("RGB", (1100, 560), "#e9e6df")
        draw = ImageDraw.Draw(sheet)
        draw.text((15, 10), display_title(mapping, variant), fill="#262626", font=font(22))
        if variant['id'] in (state.get('discarded_sets') or {}):
            draw.text((820, 12), 'SET REJECTED', fill='#a21d1d', font=font(18))
        for n, card in enumerate(variant["cards"]):
            x, y = 10 + (n % 5) * 218, 50 + (n // 5) * 250
            key = f"{variant['id']}/{card['id']}"
            if key in state["accepted"] and key not in (state.get('discarded_cards') or {}):
                with Image.open(inside(run, state["accepted"][key]["path"])) as image:
                    thumb = ImageOps.contain(image.convert("RGB"), (204, 193))
                    sheet.paste(thumb, (x + (204 - thumb.width) // 2, y + 22))
            else:
                draw.rectangle((x, y, x + 204, y + 215), fill="#cccccc")
            stars = card.get('stars', 0)
            for k in range(stars):
                cx, cy = x + 102 + (k-(stars-1)/2)*23, y+9
                points=[(cx+(11 if p%2==0 else 5)*math.cos(-math.pi/2+p*math.pi/5),
                         cy+(11 if p%2==0 else 5)*math.sin(-math.pi/2+p*math.pi/5)) for p in range(10)]
                draw.polygon(points, fill='#ffe548', outline='#db8a10')
            label = f"{n + 1:02d} {display_title(mapping, variant, card)}"
            draw.text((x, y + 220), label, fill="#262626", font=font(15))
            if key in (state.get('discarded_cards') or {}):
                draw.text((x + 8, y + 106), 'REJECTED', fill='#a21d1d', font=font(18))
        path = target / f"{variant['id']}.png"
        sheet.save(path)
        paths.append(path.relative_to(run).as_posix())
    return paths


def export(run, state, config):
    run = Path(run)
    if state.get('discarded_cards') or state.get('discarded_sets'):
        raise ValueError('Cannot export discarded cards or sets as an accepted collection.')
    full = state.get('production_contract',{}).get('mode') == 'full'
    variants = [v for v in state["plan"]["variants"]
                if full or not state.get("production_variant_ids") or v["id"] in state["production_variant_ids"]]
    if full and len(variants)!=state['production_contract']['requested_variants']:
        raise ValueError('Full delivery requires every requested variant.')
    expected = len(variants) * 10
    if len(state["accepted"]) != expected:
        raise ValueError("Cannot export an incomplete collection.")
    require_gate(state)
    if 'art_acceptance' in state:
        for key, item in state['accepted'].items():
            q = art_score(item.get('scores', {}))
            if (q < state['art_acceptance']['threshold'] or not item.get('eligible')
                    or item.get('quality') != q or state['art_stops'].get(key) != 'accepted'):
                raise ValueError(f'Cannot export image without scored acceptance: {key}')
    size = tuple(config["rules"]["output_size"])
    rows = []
    for variant in variants:
        for n, card in enumerate(variant["cards"], 1):
            key = f"{variant['id']}/{card['id']}"
            if 'allocation_policy' in state:
                item = state['accepted'][key]
                if (item.get('stars') != card['stars'] or item.get('golden') != card['golden']
                        or item.get('observed_category') != card['background']):
                    raise ValueError('Accepted art does not match its approved stars/gold/category allocation: ' + key)
            source = inside(run, state["accepted"][key]["path"])
            if sha(source) != state["accepted"][key]["sha256"]:
                raise ValueError(f"Accepted artifact changed: {key}")
            path = run / "exports" / variant["id"] / f"{n:02d}-{card['id']}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(source) as original:
                image = original.convert("RGB")
                if config["rules"]["resize_policy"] != "contain":
                    raise ValueError("Only non-cropping contain export is supported.")
                background = image.getpixel((0, 0))
                image = ImageOps.pad(image, size, method=Image.Resampling.LANCZOS, color=background)
                image.save(path)
            with Image.open(path) as check:
                if check.format != "PNG" or check.size != size:
                    raise ValueError("Export format/size verification failed.")
                check.verify()
            rows.append({"key": key, "title": card["title"], "background": card["background"],
                         "path": path.relative_to(run).as_posix(), "sha256": sha(path),
                         "source_sha256": sha(source)})
            if 'art_acceptance' in state:
                rows[-1].update(quality=state['accepted'][key]['quality'],
                                art_iteration=state['accepted'][key]['art_iteration'],
                                acceptance='accepted')
            if 'allocation_policy' in state:
                rows[-1].update(stars=card['stars'], golden=card['golden'],
                                observed_category=state['accepted'][key]['observed_category'],
                                category_reason=card['category_reason'], rarity_reason=card['rarity_reason'])
    actual = list((run / "exports").rglob("*.png"))
    if len(actual) != expected:
        raise ValueError("Unexpected extra/missing export images; inspect exports folder.")
    write(run / "exports/manifest.json", rows)
    with (run / "exports/index.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    write(run / "plans.json", state["plan"])
    write(run / "canon.json", state["canon"])
    contact_sheets(run, state, config)
    write(run / "export-check.json", {"verdict": "pass", "expected_cards": expected,
                                      "actual_cards": len(actual), "output_size": list(size),
                                      "checks": ["count", "png_decode", "dimensions", "sha256", "source_unchanged"]})
    event(run, "gate_result", gate="export", verdict="pass", cards=len(rows))


def report(run, state):
    run = Path(run)
    mapping = names(run)
    esc = lambda value: html.escape(str(value))
    href = lambda path: quote(str(path).replace("\\", "/"), safe="/.-_")
    content = ["<!doctype html><html lang='ru'><meta charset='utf-8'>",
               "<meta name='viewport' content='width=device-width,initial-scale=1'>",
               "<title>Setforge — отчёт запуска</title><style>",
               "body{font:16px/1.5 system-ui;max-width:1200px;margin:32px auto;padding:0 20px;color:#222;background:#faf9f6}",
               "h1,h2,h3{line-height:1.2}img{max-width:100%;height:auto}article{margin:26px 0;padding:18px;background:#fff}",
               "pre{white-space:pre-wrap;overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}",
               "td,th{text-align:left;padding:9px;border-bottom:1px solid #ddd;vertical-align:top}",
               ".fail,.error,.needs_attention,.discarded,.completed_with_rejections{color:#a21d1d}.pass,.complete{color:#12652e}",
               ".waiting_for_worker,.uncertain{color:#8a5b00}.history{max-height:280px;width:auto}",
               "a{color:#214e99}summary{cursor:pointer}</style>",
               "<h1>Setforge — отчёт запуска</h1>",
               f"<p><b>{esc(read(run / 'inputs/brief.json')['theme'])}</b></p>",
               f"<p>Статус: <b class='{esc(state['status'])}'>{esc(state['status'])}</b>. {esc(state['reason'])}</p>",
               f"<p>Заданий: {state['calls']}. Оценка затрат по настройкам: {state['estimated_cost']:.2f}.",
               " Это оценка, не подтверждённый счёт провайдера.</p>",
               "<p><a href='inputs/brief.json'>Бриф</a> · <a href='inputs/project.json'>Настройки</a> · ",
               "<a href='events.jsonl'>История событий</a> · <a href='state.json'>Состояние</a></p>"]
    if state["status"] == "complete" and not state.get('discarded_cards') and not state.get('discarded_sets'):
        content.append("<p><a href='exports/manifest.json'>Файлы и контрольные суммы</a> · <a href='export-check.json'>Проверка экспорта</a></p>")
    if 'allocation_policy' in state:
        gate = state.get('allocation_gate')
        content.append(f"<p><b>Распределение до арта:</b> {esc('проверено' if gate else 'ещё не принято')}. "
                       "У каждой карточки обязательны звёзды, признак золота, категория и причины выбора. "
                       "Категории 1–4 обязательны независимо от звёзд. "
                       "<a href='inputs/allocation-policy.json'>Политика</a></p>")
        if gate:
            content.append("<p><a href='allocation.json'>Утверждённая матрица категорий, звёзд и персонажей</a></p>")
    if 'art_acceptance' in state:
        content.append(f"<p>{esc(acceptance_text(state))} "
                       "<a href='inputs/acceptance-policy.json'>Исходный снимок политики</a>; "
                       "действующая версия — в состоянии запуска и истории поправок.</p>")
        content.append('<table><tr><th>Карточка</th><th>Решение</th><th>Итерация</th><th>Q95</th></tr>')
        for key, history in state.get('art_history', {}).items():
            if not history:
                continue
            r = history[-1]
            decision='Отбракована · не принята' if key in (state.get('discarded_cards') or {}) else state.get('art_stops', {}).get(key)
            content.append(f"<tr><td>{esc(key)}</td><td>{esc(decision)}</td>"
                           f"<td>{r['iteration']}</td><td>{r['quality']:.4f}</td></tr>")
        content.append('</table>')
    for variant in (state["plan"] or {}).get("variants", []):
        title = display_title(mapping, variant)
        content.append(f"<article><h2>{esc(title)}</h2><p>{esc(variant['premise'])}</p><p>{esc(variant['difference'])}</p>")
        if variant['id'] in (state.get('discarded_sets') or {}):
            content.append(f"<p class='discarded'><b>Набор отклонён.</b> {esc(discard_reason(state['discarded_sets'][variant['id']]))} "
                           "Набор требует десять принятых карточек.</p>")
        planned_only = bool(state.get("production_variant_ids") and variant["id"] not in state["production_variant_ids"])
        if planned_only:
            content.append("<p>Вариант состава: оставлен без генерации арта после выбора другого варианта.</p>")
        preview = f"previews/{variant['id']}.png"
        if not planned_only and (run / preview).exists():
            content.append(f"<img src='{href(preview)}' alt='{esc(title)} — 10 карточек'>")
        content.append("<table><tr><th>Карточка</th><th>Роль / фон</th><th>Иллюстрация</th></tr>")
        for card in variant["cards"]:
            key = f"{variant['id']}/{card['id']}"
            discarded = key in (state.get('discarded_cards') or {})
            artifact = state["accepted"].get(key) if not discarded else None
            link = f"<a href='{href(artifact['path'])}'>PNG</a>" if artifact else ("Состав без арта" if planned_only else "Не принята")
            if discarded:
                link = f"<span class='discarded'>Отбракована · не принята.</span> {esc(discard_reason(state['discarded_cards'][key]))}"
                latest = state.get('latest', {}).get(key)
                if latest:
                    link += f" <a href='{href(latest['path'])}'>Последний отклонённый PNG</a>"
            if not artifact and qualified_entities(state,key):
                link += '<br>Образ сущности принят отдельно для канона; карточка не принята.'
            if 'allocation_policy' in state:
                link += f"<br>{card['stars']}★; {'золотая' if card['golden'] else 'обычная'}"
            content.append(f"<tr><td>{esc(display_title(mapping, variant, card))}</td><td>{esc(card['semantic_role'])}; фон {card['background']}</td><td>{link}</td></tr>")
        content.append("</table></article>")
    qualified=[entity for entity in state.get('canon',[]) if entity.get('qualification')=='entity_only']
    if qualified:
        content.append('<h2>Отдельная приёмка образов для канона</h2><p>Решение относится только к указанным компонентам сущности, а не к приёмке всей карточки-источника.</p>')
        for entity in qualified:
            source=entity.get('identity_source_artifact',{})
            key=entity.get('established_by') or entity.get('source_card_id')
            card_ok=key in state.get('accepted',{}) and key not in (state.get('discarded_cards') or {})
            source_link=f"<a href='{href(source['path'])}'>Точная версия образа</a>" if source.get('path') else 'Источник не указан'
            content.append(f"<p><b>{esc(entity['id'])}</b> · {source_link} · карточка {esc(key)}: {'принята' if card_ok else 'не принята'}. "
                           f"Проверка: {esc(entity.get('identity_review_job'))}. SHA256: {esc(source.get('sha256'))}.<br>"
                           f"Принятые компоненты: {esc(', '.join(entity.get('approved_components',[])))}.<br>"
                           f"Исключённые компоненты: {esc(', '.join(entity.get('excluded_components',[])) or 'не указаны')}.</p>")
    if state.get("selection"):
        selection = state["selection"]
        content.append(f"<p><b>Выбран вариант: {esc(selection['variant_id'])}</b>. {esc(selection['note'])}</p>")
    content.append("<h2>Проверки и итерации</h2>")
    for job in state["jobs"]:
        folder = run / "jobs" / job["id"]
        response = read(folder / "response.json") if (folder / "response.json").exists() else {}
        verdict = response.get("verdict", job["status"])
        scored = next((r for history in state.get('art_history', {}).values()
                       for r in history if r['review_job'] == job['id']), None)
        if scored:
            verdict = 'pass' if scored['eligible'] and scored['quality'] >= state['art_acceptance']['threshold'] else 'fail'
        content.append(f"<details><summary><b class='{esc(verdict)}'>{esc(verdict)}</b> — {esc(job['id'])}: {esc(job['task_key'])}</summary>")
        content.append(f"<p>{esc(job.get('executor', job['provider']['type']))}; {esc(job.get('model', job['provider'].get('model', 'unspecified')))}</p>")
        if response.get("summary"):
            content.append(f"<p>{esc(response['summary'])}</p>")
        if (folder / "artifact.png").exists():
            content.append(f"<img class='history' src='{href(folder.relative_to(run) / 'artifact.png')}' alt='Итерация иллюстрации'>")
        if job.get("reason"):
            content.append(f"<p>{esc(job['reason'])}</p>")
        content.append(f"<p><a href='{href(folder.relative_to(run) / 'request.json')}'>Запрос</a>")
        if response:
            content.append(f" · <a href='{href(folder.relative_to(run) / 'response.json')}'>Ответ</a>")
            content.append(f"<pre>{esc(json_text(response))}</pre>")
        content.append("</p></details>")
    content.append("</html>")
    (run / "audit.html").write_text("\n".join(content), encoding="utf-8")
    from .showcase import render
    render(run,state,read(run/'inputs/brief.json'))


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False, indent=2)
