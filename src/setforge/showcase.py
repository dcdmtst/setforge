"""Offline producer gallery with reversible presentation layers."""
import html
from pathlib import Path
from urllib.parse import quote
from .io import inside
from .naming import names, display_title
from .ui_skin import label_svg, star_svg, measure_label
from .nameplate import preflight, DEFAULT_POLICY
from .timing import collect_timing, render_timing
from .io import read,write
import copy

STAR=star_svg()

def stars_svg(value):
    if not isinstance(value,int) or not 1<=value<=5:
        return '<span class="unassigned">Звёзды не назначены</span>'
    return f'<span class="star-group" role="img" aria-label="{value} stars" data-stars="{value}">{STAR*value}</span>'


def acceptance_text(state):
    """Describe this run's effective policy, including historical snapshots."""
    policy = state.get('art_acceptance')
    if not policy:
        return 'Для этого исторического запуска числовая политика приёмки не задана.'
    threshold = str(policy.get('threshold', 93.5)).replace('.', ',')
    version = policy.get('version', 1)
    edits = policy.get('max_edits', 5 if version == 1 else 3)
    edit_word = 'переделок'
    if isinstance(edits, int) and not 11 <= edits % 100 <= 14:
        edit_word = 'переделка' if edits % 10 == 1 else ('переделки' if edits % 10 in (2, 3, 4) else 'переделок')
    base = (f'Приёмка арта: Q95 ≥{threshold} без округления и все обязательные гейты. '
            f'Максимум {edits} {edit_word} после исходника. ')
    if version == 1:
        return base + 'Историческая политика: остановка на плато или лимите ниже порога не считается приёмкой.'
    return base + ('Плато требует сменить способ исправления, без досрочной остановки. '
                   'После лимита непринятая карточка отбраковывается; набор без десяти '
                   'принятых карточек отклоняется. Отдельно принятый образ сущности '
                   'не принимает исходную карточку.')


def discard_reason(value):
    if isinstance(value, dict):
        return value.get('reason') or value.get('summary') or 'Не выполнены условия приёмки.'
    return str(value) if value else 'Не выполнены условия приёмки.'


def qualified_entities(state, key):
    return [entity for entity in state.get('canon', [])
            if entity.get('qualification') == 'entity_only'
            and (entity.get('established_by') or entity.get('source_card_id')) == key]


def label_review(run,state,mapping):
    shown=copy.deepcopy(state.get('plan') or {'variants':[]})
    for variant in shown['variants']:
        for card in variant['cards']:card['title']=display_title(mapping,variant,card)
        variant['title']=display_title(mapping,variant)
    note=read(run/'presentation-naming-notes.json') if (run/'presentation-naming-notes.json').exists() else {}
    policy={**DEFAULT_POLICY,'producer_flagged_labels':note.get('producer_flagged_labels',[])}
    result=preflight(shown,policy,measure_label)
    write(run/'nameplate-report.json',result)
    failed=[x for x in result['labels'] if x['verdict']!='pass']
    esc=lambda x:html.escape(str(x),quote=True)
    body='<section class="label-review" id="nameplate-review"><h2>Подписи: проверка до генерации</h2><p>Текущие названия сохранены. Фиксированный размер букв — 18 px, стандартная ширина подписи — 160 px. Размер не подстраивается под слово. Сначала сокращаем название; если подходящего названия нет — меняем карточку до арта. Для названия набора отдельно заданы 31 px и 850 px.</p>'
    if failed:
        body+='<p><b>Арт принят отдельно. Эти подписи ещё требуют правки:</b></p><table><thead><tr><th>Подпись</th><th>Ширина</th><th>Доступно</th><th>Статус</th></tr></thead><tbody>'+''.join(f'<tr><td>{esc(x["text"])}</td><td>{esc(x["measured_width"])} px</td><td>{x["available_width"]} px</td><td class="no">Требует правки</td></tr>' for x in failed)+'</tbody></table>'
    else:body+='<p>Строки помещаются при стандартном размере. Проверка узких окон дополнительно выполняется в браузере.</p>'
    body+='</section><details class="ui-source"><summary>Оформление публичной версии</summary><p>'+STAR+' Нейтральные звёзды построены из SVG, подписи используют встроенный в Pillow шрифт Aileron. Графика и шрифты из игровых скриншотов не распространяются. Английские названия проверяются в этом оформлении; при смене шрифта нужна повторная проверка. Локализации и loc QA — out of scope.</p></details>'
    return body,result

CSS='''
:root{--ink:#203a36;--muted:#657672;--paper:#f5f3ec;--line:#dce2d9}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 Arial,sans-serif}
main{max-width:1240px;margin:auto;padding:34px 28px}h1{font-size:34px;line-height:1.2;margin:8px 0 20px}h2,h3{margin:0}p{margin:8px 0}a{color:#17634f}
.eyebrow{font-size:12px;letter-spacing:2px;text-transform:uppercase;color:var(--muted)}.stats{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}.stat{padding:12px 20px;background:#fff;border:1px solid var(--line);border-radius:12px}.stat b{display:block;font-size:26px}.stat span,.muted{font-size:13px;color:var(--muted)}
.brief,.notice{padding:16px 20px;border-radius:12px;border:1px solid var(--line);background:#fff;margin:18px 0}.brief h2{font-size:16px}.brief p{white-space:pre-wrap}.notice{background:#fff3d6}
.controls{position:sticky;top:0;z-index:10;display:flex;flex-wrap:wrap;gap:12px 24px;align-items:center;background:#fffcf5f5;backdrop-filter:blur(8px);padding:16px 20px;border:1px solid #cdd9ce;border-radius:12px;box-shadow:0 4px 15px #1235260d}.controls label{display:flex;gap:8px;align-items:center;font-size:14px;cursor:pointer}.controls input{width:18px;height:18px;accent-color:#148a4c}.qa-status{font-size:12px;color:#556d61;width:100%}
nav{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0}nav a{padding:8px 14px;background:#e3ebe1;border-radius:24px;text-decoration:none;font-size:14px}
.variant{margin:30px 0 56px;scroll-margin-top:140px}.variant-info{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-bottom:14px}.variant-info p{max-width:860px;font-size:14px}.badge{border-radius:30px;background:#e7e9e3;padding:6px 12px;white-space:nowrap;font-size:12px}.badge.ready{background:#d8eadf;color:#205c4d}.explain{font-size:13px;color:#657672}summary{cursor:pointer}details{margin:10px 0}
.board-scroll{overflow:auto;padding:8px 8px 16px}.collection-board{--tone:#0b9b50;--deep:#07552f;--light:#5fe18a;padding:22px;border-radius:20px;background:#fff;border:1px solid var(--line)}
.set-banner{margin:0 auto 24px;text-align:center;padding:12px}.set-banner h2{font:bold 28px Arial,sans-serif;line-height:1.15;white-space:nowrap}.cards{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:28px 16px}
.card{min-width:0;position:relative;background:transparent}.card-shell{padding:6px;border-radius:22px;background:linear-gradient(145deg,#f5fcff 0%,#91bae0 25%,#e3f6ff 49%,#7c9bcf 72%,#5066aa 100%);box-shadow:0 3px 4px #15284d70,inset 0 0 0 1px #def7ff}.card.golden .card-shell{background:linear-gradient(145deg,#fff4a9,#efb925 28%,#ffe58b 48%,#d88a14 76%,#986209)}
.card-inner{border:2px solid #e7f8f8;border-radius:17px;overflow:hidden;background:#155d3e}.golden .card-inner{border-color:#fff2ad}.art{aspect-ratio:43/48;position:relative;background:#e7ece5}.art>a{display:block;width:100%;height:100%}.art img{display:block;width:100%;height:100%;object-fit:contain}.placeholder{height:100%;display:grid;place-content:center;text-align:center;font-size:12px;color:#65786c}.placeholder b{font-size:30px;display:block}
.nameplate{height:32px;display:flex;align-items:center;justify-content:center;padding:0 4px;background:var(--deep);border-top:1px solid #e8f9ec70}.card-name{font:900 clamp(13px,1.4vw,18px)/1 Arial,sans-serif;white-space:nowrap;text-transform:uppercase;color:white;text-shadow:1px 2px 0 #17362b,-1px -1px 0 #17362b;letter-spacing:-.35px;max-width:100%}
.rarity-row{position:absolute;left:0;right:0;top:-17px;z-index:2;display:flex;justify-content:center;pointer-events:none}.star-group{display:inline-flex;align-items:center;gap:0;white-space:nowrap;filter:drop-shadow(0 2px 1px #91591870)}.star{width:clamp(21px,2.4vw,30px);height:clamp(21px,2.4vw,30px);flex:none;overflow:visible}.unassigned{font-size:11px;background:#fff3d6;color:#73582c;padding:3px}
.card-body{padding:10px 2px 0;font-size:12px;color:#63756b}.status.accepted{color:#206147}.strip{position:absolute;bottom:0;width:100%;background:#fff0ce;padding:5px;text-align:center;font-size:12px}.set-footer{text-align:center;font-weight:bold;font-size:14px;color:#856045;margin-top:18px}.metadata{margin-top:12px}.metadata table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}.metadata td,.metadata th{text-align:left;padding:8px;border-bottom:1px solid var(--line)}.metadata .star{width:19px;height:19px}.metadata .star-group{filter:none}
.final-view .collection-board{min-width:860px;max-width:1060px;margin:auto;padding:0 30px 18px;border:8px solid #fae0a7;border-radius:36px;background:repeating-linear-gradient(55deg,#ffffff04 0 35px,#00000004 35px 70px),var(--tone);box-shadow:0 0 0 3px var(--deep),0 0 0 7px var(--light),0 8px 15px #0a392e30,inset 0 0 0 3px #e7b86b}
.final-view .set-banner{margin:-5px -38px 34px;padding:18px 20px 20px;border:2px solid var(--light);border-radius:42% 42% 12% 12% / 22% 22% 22% 22%;background:linear-gradient(#ffffff15,#0000000b),var(--tone);box-shadow:0 5px 0 var(--deep)}.final-view .set-banner h2{font-weight:1000;font-size:32px;letter-spacing:.5px;color:#ffdf48;text-shadow:1px 3px 0 #68541e,-1px -1px 0 #7c6927;text-transform:uppercase}
.final-view .card-body{display:none}.final-view .set-footer{display:table;margin:22px auto -13px;padding:4px 28px;background:#fae0a7;border-radius:20px 20px 0 0;color:#945534}
.hide-names .display-name{visibility:hidden}.hide-stars .rarity-row,.hide-stars .metadata .star-group{visibility:hidden}.hide-frames .card-shell{background:none;box-shadow:none;padding:6px}.hide-frames .card-inner{border-color:transparent;border-radius:0}.hide-frames .nameplate{border-color:transparent}.hide-frames .art{border-radius:0}.fit-fail{outline:3px solid #f85241!important}
.footer{margin-top:30px;padding-top:18px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}.footer a{margin-right:18px}.badge.discarded,.strip.discarded{background:#ffe0da;color:#942b21}.status.discarded{color:#942b21}.discard-reason{font-size:13px;color:#942b21;margin:8px 0 16px}.identity-note{font-size:12px;color:#657672}
.card-inner{overflow:visible}.art{overflow:hidden;border-radius:14px 14px 0 0}.nameplate{height:34px;border-radius:0 0 14px 14px;overflow:visible}
.card-name{font-size:18px;line-height:1;letter-spacing:0;text-transform:none;text-shadow:none;max-width:none;flex:none}.game-type{display:block;flex:none;overflow:visible}
.set-banner h2{display:flex;justify-content:center;align-items:center;line-height:1;height:32px}.set-banner .game-type{filter:sepia(1) saturate(5)}
.final-view .collection-board{width:1060px;min-width:1060px;max-width:1060px}
.star-group[data-stars="5"]>.star:nth-child(1){transform:translateY(4px) rotate(-16deg)}.star-group[data-stars="5"]>.star:nth-child(2){transform:rotate(-8deg)}.star-group[data-stars="5"]>.star:nth-child(3){transform:translateY(-2px)}.star-group[data-stars="5"]>.star:nth-child(4){transform:rotate(8deg)}.star-group[data-stars="5"]>.star:nth-child(5){transform:translateY(4px) rotate(16deg)}
.hide-names .fit-fail{outline:none!important}.label-review{margin:24px 0;padding:20px;background:#fff3d6;border:1px solid #e4c78d;border-radius:12px}.label-review h2{font-size:21px}.label-review table{width:100%;border-collapse:collapse;font-size:13px}.label-review td,.label-review th{text-align:left;padding:9px;border-bottom:1px solid #e3d9bc}.label-review .no{color:#9a3422}.ui-source{margin-top:24px;font-size:13px;color:#50655b}.ui-source .star{display:inline-block;vertical-align:middle;width:32px;height:32px}
@media(max-width:900px){main{padding:22px 14px}body:not(.final-view) .cards{grid-template-columns:repeat(3,minmax(0,1fr))}.variant-info{align-items:flex-start}.controls{position:relative}}
@media(max-width:540px){h1{font-size:28px}body:not(.final-view) .cards{grid-template-columns:repeat(2,minmax(0,1fr))}body:not(.final-view) .collection-board{padding:12px}.controls{gap:12px}.controls label{font-size:12px}.variant-info{display:block}.set-banner h2{font-size:24px}}
'''

JS='''<script>
(()=>{
const opts={names:'hide-names',frames:'hide-frames',stars:'hide-stars'};
function inspect(){
const measurements=[...document.querySelectorAll('.card-name,.set-banner h2')].map(el=>{
const range=document.createRange();range.selectNodeContents(el);
const glyph=el.querySelector('svg.game-type');
const rect=glyph?glyph.getBoundingClientRect():range.getBoundingClientRect(),box=el.parentElement.getBoundingClientRect(),style=getComputedStyle(el);
const fits=rect.width<=box.width-8+.5 && rect.height<=box.height+.5;
el.classList.toggle('fit-fail',!fits);
return {key:el.closest('[data-key]')?.dataset.key||el.closest('[data-variant]').dataset.variant,title:el.textContent,width:rect.width,available:box.width-8,font_size:parseFloat(style.fontSize),fits};
});
window.setforgeDisplayQA={version:3,language:'en',viewport:innerWidth,final_view:document.body.classList.contains('final-view'),passed:measurements.every(x=>x.fits),measurements};
const fails=measurements.filter(x=>!x.fits);
document.querySelector('.qa-status').textContent=fails.length?'Подписи требуют правки: '+fails.map(x=>x.title).join(', '):'Подписи помещаются в текущем размере окна; замечания продюсера сохранены ниже.';
}
document.querySelectorAll('.controls input').forEach(input=>input.addEventListener('change',()=>{
if(input.id==='show-final')document.body.classList.toggle('final-view',input.checked);
else document.body.classList.toggle(opts[input.name],!input.checked);
requestAnimationFrame(inspect);
}));
document.fonts.ready.then(inspect);addEventListener('resize',inspect);new ResizeObserver(inspect).observe(document.querySelector('main'));
})();</script>'''

def render(run,state,brief,*,audit_path='audit.html',notice=''):
    run=Path(run);mapping=names(run)
    esc=lambda x:html.escape(str(x),quote=True)
    href=lambda x:quote(str(x).replace('\\','/'),safe='/.-_')
    variants=(state.get('plan') or {}).get('variants',[])
    requested=brief.get('variants',3);accepted=state.get('accepted',{});latest=state.get('latest',{})
    discarded_cards=state.get('discarded_cards') or {}
    discarded_sets=state.get('discarded_sets') or {}
    def artifact(key):
        a=accepted.get(key) or latest.get(key)
        if not a:return None
        try:return a if inside(run,a['path']).is_file() else None
        except ValueError:return None
    def ready(key):return key not in discarded_cards and key in accepted and artifact(key) is not None
    ready_cards=sum(ready(f"{v['id']}/{c['id']}") for v in variants for c in v['cards'])
    ready_sets=sum(v['id'] not in discarded_sets and len(v['cards'])==10 and all(ready(f"{v['id']}/{c['id']}") for c in v['cards']) for v in variants)
    page=[f'<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(brief["theme"])} — варианты наборов</title><style>{CSS}</style><body class="final-view"><main>',
          '<div class="eyebrow">Setforge / результаты заказа</div>',f'<h1>{esc(brief["theme"])}</h1>',
          f'<div class="stats"><div class="stat"><b>{requested}</b><span>заказано вариантов</span></div><div class="stat"><b>{ready_sets} / {requested}</b><span>наборов с готовым артом</span></div><div class="stat"><b>{ready_cards} / {requested*10}</b><span>готовых карточек</span></div></div>',
          '<section class="brief"><h2>Свободные пожелания продюсера</h2>',f'<p>{esc(brief.get("wishes") or "Дополнительные пожелания не указаны.")}</p></section>',
          '<fieldset class="controls"><legend>Отображение</legend>'+''.join(f'<label><input type="checkbox" id="show-{key}" name="{key}" checked aria-controls="collections">{label}</label>' for key,label in [('names','Отображать названия'),('frames','Отображать рамки'),('stars','Отображать звёздочки'),('final','Итоговое отображение')])+'<span class="qa-status" role="status">Проверка английских подписей…</span></fieldset>']
    if notice:page.append(f'<p class="notice">{esc(notice)}</p>')
    if discarded_sets or state.get('status')=='completed_with_rejections':
        page.append(f'<p class="notice">Отклонено наборов: {len(discarded_sets)}. Полный заказ не готов; принятые карточки ниже не означают приёмку неполного набора.</p>')
    if requested!=len(variants):page.append(f'<p class="notice">Составлено {len(variants)} из {requested} вариантов. Заказ ещё не готов.</p>')
    page.append('<nav aria-label="Варианты набора">'+''.join(f'<a href="#set-{esc(v["id"])}">{i}. {esc(display_title(mapping,v))}</a>' for i,v in enumerate(variants,1))+'</nav><div id="collections">')
    tones=[('#149752','#075c36','#45ce7c'),('#2785bd','#18517f','#70bbeb'),('#ba662b','#733e25','#e9a45b')]
    for i,v in enumerate(variants,1):
        title=display_title(mapping,v);n=sum(ready(f"{v['id']}/{c['id']}") for c in v['cards'])
        discarded=v['id'] in discarded_sets
        set_ready=n==10 and len(v['cards'])==10 and not discarded
        badge_class='discarded' if discarded else ('ready' if set_ready else '')
        badge_text='Набор отклонён' if discarded else ('Арт готов' if set_ready else 'Арт не готов')
        tone,deep,light=tones[(i-1)%len(tones)]
        page += [f'<section class="variant" id="set-{esc(v["id"])}" data-variant="{esc(v["id"])}" data-ready="{n}">',
                 f'<div class="variant-info"><p>{esc(v["premise"])}</p><span class="badge {badge_class}">{badge_text} · {n}/10</span></div>']
        if discarded:page.append(f'<p class="discard-reason">{esc(discard_reason(discarded_sets[v["id"]]))}</p>')
        page.append(f'<div class="board-scroll"><div class="collection-board" style="--tone:{tone};--deep:{deep};--light:{light}"><header class="set-banner"><h2 class="display-name" lang="en">{label_svg(title,31)}</h2></header><div class="cards">')
        metadata=[]
        for j,c in enumerate(v['cards'],1):
            key=f"{v['id']}/{c['id']}";a=artifact(key);ok=ready(key);ct=display_title(mapping,v,c)
            card_discarded=key in discarded_cards
            art=(f'<a href="{href(a["path"])}" aria-label="Open {esc(ct)}"><img loading="lazy" src="{href(a["path"])}" alt="{esc(ct)}"></a>' if a else f'<div class="placeholder"><b>{j:02d}</b>Арт ещё не создан</div>')
            if card_discarded:art+='<div class="strip discarded">Отбракована · не принята</div>'
            elif a and not ok:art+='<div class="strip">Кандидат · не принят</div>'
            stars=c.get('stars',a.get('stars') if a else None)
            status='Отбракована · не принята' if card_discarded else ('Принята' if ok else ('Не принята' if a else 'Состав без арта'))
            status_class='discarded' if card_discarded else ('accepted' if ok else '')
            q=f" · Q95 {a['quality']:.2f}" if a and 'quality' in a else ''
            reason=f'<div>{esc(discard_reason(discarded_cards[key]))}</div>' if card_discarded else ''
            if not ok and qualified_entities(state,key):
                reason+='<div class="identity-note">Образ сущности принят отдельно для канона. Карточка не принята.</div>'
            page.append(f'<article class="card {"golden" if c.get("golden") else ""}" data-key="{esc(key)}"><div class="rarity-row">{stars_svg(stars)}</div><div class="card-shell"><div class="card-inner"><div class="art">{art}</div><div class="nameplate"><h3 class="card-name display-name" lang="en">{label_svg(ct,18)}</h3></div></div></div><div class="card-body"><div>Категория {c["background"]}{" · золотая" if c.get("golden") else ""}{q}</div><div class="status {status_class}">{status}</div>{reason}</div></article>')
            metadata.append(f'<tr><td lang="en">{esc(ct)}</td><td>{stars_svg(stars)}</td><td>{c["background"]}</td><td>{"Золотая" if c.get("golden") else "Обычная"}</td><td>{status}{q}{reason}</td></tr>')
        page.append(f'</div><footer class="set-footer">SET {i} / {requested} · {n} CARDS ACCEPTED{" · SET REJECTED" if discarded else ""}</footer></div></div>')
        page.append('<details class="metadata"><summary>Категории, редкость и приёмка</summary><table><thead><tr><th>Название</th><th>Звёзды</th><th>Категория</th><th>Тип</th><th>Приёмка</th></tr></thead><tbody>'+''.join(metadata)+'</tbody></table></details>')
        if v.get('wishes_response'):page.append(f'<details class="explain"><summary>Как учтены пожелания</summary><p>{esc(v["wishes_response"])}</p></details>')
        page.append('</section>')
    labels,naming=label_review(run,state,mapping)
    page.append('</div>'+labels)
    page.append(f'<div class="footer"><p>{esc(acceptance_text(state))}</p><p>Переполнение помечается, а не скрывается уменьшением шрифта или обрезанием. Рамки, подписи и звёзды — отдельные слои просмотра; исходные иллюстрации сохранены.</p>')
    if state.get('status')=='complete' and not state.get('delivery_view'):
        page.append('<a class="download-zip" href="delivery.zip" download>Скачать ZIP</a><a href="result-manifest.json">Манифест результата</a>')
    if audit_path:page.append(f'<a href="{href(audit_path)}">Проверки и история</a>')
    if (run/'review.html').exists():page.append('<a href="review.html">Оценки арта</a>')
    if (run/'naming-review.json').exists():page.append('<a href="naming-review.json">Проверка названий</a>')
    if not discarded_sets and not discarded_cards and (run/'exports/manifest.json').exists():page.append('<a href="exports/manifest.json">Все отдельные PNG</a>')
    page.append('</div>')
    write(run/'timing-data.json',collect_timing(run,state))
    page.append(render_timing(run,state))
    page.append('</main>'+JS+'</body></html>')
    (run/'report.html').write_text('\n'.join(page),encoding='utf-8')
    naming_required=bool(state.get('nameplate_policy') or (run/'presentation-naming-notes.json').exists())
    return {'requested_variants':requested,'visible_variants':len(variants),'ready_sets':ready_sets,'ready_cards':ready_cards,'expected_cards':requested*10,'discarded_cards':len(discarded_cards),'discarded_sets':len(discarded_sets),'nameplate_qa':naming['verdict'],'full_delivery_ready':not discarded_sets and not discarded_cards and ready_sets==requested and ready_cards==requested*10 and (not naming_required or naming['verdict']=='pass')}
